# Paper Pipeline Refactor Workshop

This is the working document for designing the next version of Paper Pipeline.

It is intentionally not yet a detailed implementation plan. We first want to
understand the product, its important features, and its plausible future
directions. Concrete schemas, directory layouts, frameworks, and module
boundaries should follow from those decisions instead of constraining them.

## How to Use This Document

- Record confirmed decisions separately from ideas and open questions.
- Prefer describing user capabilities before proposing implementation details.
- Keep possible future features visible when they could affect foundational
  choices, without requiring the first release to implement them.
- Move durable architectural decisions into ADRs once the design becomes
  concrete.
- Treat this as a workshop document. It is expected to change substantially.

# 1. Basic Context

## What Paper Pipeline Is

Paper Pipeline turns a collection of academic papers into a local,
folder-based library that both humans and LLM agents can browse and use.

The motivating observation is that agents are unreliable when asked to recall
the literature, but are much more useful when they can search dependable local
sources with ordinary tools such as `rg`. PDFs are valuable source documents,
but they are a poor primary interface for that kind of agent workflow.

Zotero is currently the starting point for collecting papers. Paper Pipeline
is responsible for turning that collection into durable, machine-readable
content and for helping a user inspect and operate the resulting library.

## Why the Old Version Is Being Replaced

The old version proved the basic idea, but its design was too closely shaped by
its first implementation:

- Nougat became the center of the pipeline instead of one possible document
  conversion technology.
- Metadata, processing state, generated files, and UI state did not have clear
  ownership.
- Adding new workflows required workflow-specific state and UI code.
- The browser UI mixed several state-management approaches and was easy for
  agents to break while extending it.
- The project lacked the tests and operational guidance needed for reliable
  agent-driven development.

The old code now lives under `v1/` as reference material only.

## Confirmed Direction

- This is a greenfield redevelopment.
- Existing paper databases have been deleted and do not need migration support.
- The initial product is personal, local software rather than a commercial or
  multi-tenant service.
- The library should remain inspectable through ordinary files and tools.
- A browser-based UI is desirable, but its implementation must be much harder
  to accidentally break.
- Automated tests and strong agent operating instructions are first-class
  project requirements. 
- Dev ops is heavily in scope for this document.

Greenfield development does not eliminate the need to think about future
schema evolution. It only means that compatibility with the v1 database is not
a requirement.

## Product Principles

These are working principles, not yet formal architecture decisions:

1. **Agent Search.** The system should make it easy for agents to
   find and cite source material instead of relying on remembered facts.
2. **Agent-friendly and human-friendly.** Files should work well with search
   tools while the UI provides a good overview and safer operations.
3. **Reliable extension.** Adding a capability should have an obvious home and
   should not require synchronized edits across unrelated layers.
4. **Derived features stay rebuildable.** Search indexes, summaries, and other
   generated views should not become irreplaceable sources of truth.
5. **Complexity must earn its place.** Future possibilities should inform the
   design, but should not force premature infrastructure into the first useful
   version.

## Questions That Define the Product

These need answers before a concrete architecture is chosen:

- Is Paper Pipeline primarily a library builder, a research workbench, or both?

Paper pipeline is exclusively a library builder.

- Is Zotero only an import source, or should Paper Pipeline maintain ongoing
  synchronization with it?

Its hard to say, but I think zotero should just be treated as an import source.
Now during research we do always add more papers to a zotero library so we need a way to add more.

- Should a library own copies of source PDFs, reference Zotero-managed files,
  or support both modes?

Own copies of the source PDFs.

- Is the main agent interface the filesystem, a CLI, an MCP server, or some
  combination?

The main agent interface is the filesystem.
Its a very pure setup:

<library folder>/
    AGENTS.md (Index meta info.).
    <citekey>/
        stuff
    <citekey>/
        stuff

- Are generated outputs disposable artifacts, reviewable publications, or
  starting points for user-authored notes?

Generated outputs are artifacts for AI to look at.
A person could (if they wanted to) just edit the files on disk, but its not the expectation.

- How important is portability between machines?

The generated result should be portable in the sense you can copy paste it other places.

- Should a library be usable without running Paper Pipeline at all?

The library itself is strictly independent of paper pipeline.
It should be a compmpletely independent artifact that anyone can copy paste onto their computers and tell agents to look at.

# 2. Feature Plans

This section is a capability inventory. Items are candidates until they are
assigned to a release or explicitly rejected.

## Core User Journey

A likely basic journey is:

1. Point Paper Pipeline at a Zotero collection or export.
2. Review what will be added or changed.
3. Build or update a local paper library.
4. Convert source documents into searchable, structured content.
5. Inspect conversion quality and rerun failures when necessary.
6. Search and browse the library from the filesystem, CLI, UI, or an agent.
7. Run optional analysis or extraction workflows over selected papers.

This journey is a hypothesis to refine, not yet a commitment to a particular
workflow or screen layout.

## Library Creation and Maintenance

Candidate capabilities:

- Import bibliographic metadata and attachments from Zotero.
- Preview an import before changing the library.
- Add new papers and refresh changed metadata.
- Detect missing, moved, changed, or duplicate source files.
- Handle changed citekeys without silently duplicating a paper.
- Define what happens when a paper disappears from a later Zotero export.
- Validate and repair an inconsistent library.
- Explain which files are managed by Paper Pipeline and which are user-owned.

Open questions:

- Which Zotero input should come first: RDF, Better BibTeX JSON, a live Zotero
  API, or another format?

RDF is probably fine for now.

- Does “update from Zotero” replace local metadata, merge it, or present a
  reviewable diff?

Replace is probably best for now.

- Does Paper Pipeline eventually need to ingest non-Zotero papers?

Out of scope for now, but it might be nice to abstract over what we get from zotero.

## Document Conversion

Candidate capabilities:

- Convert PDFs into high-quality Markdown.

This is one of the central features. At least one of "stuff" should be a transcription.md

- Preserve useful document structure such as sections, pages, equations,
  tables, figures, captions, and references where the backend supports it.

Part of being high quality markdown is to not bung up stuff.

- Preserve extracted figures and other assets.

Extracted figures might be useful to keep. A possible outline of what a paper extraction looks like could include:
<citekey>/
    figures/
        something.png
        something.png
    page_images/
        pg_1.png
        pg_2.png
        etc.

- Retain enough raw output for debugging and future reprocessing.



- Support scanned and native-text PDFs.

Hopefully.

- Show conversion configuration, backend version, timing, and errors.
- Re-run conversion with different settings or a different backend.
- Compare or review conversion quality.

These sound interesting but perhaps lets design such that these don't become too burdensome to add later.

- Process one paper or a selected batch.

Marker is the leading candidate to replace Nougat, but the feature requirement
is good document conversion rather than “run Marker.” A small representative
paper corpus should be used to validate Marker and any serious alternatives.

Open questions:

- Is Markdown alone sufficient for agents, or should structured blocks also be
  a standard output?

Markdown alone is sufficient, but will include more output as well.

- How much page-level provenance must survive conversion?

I don't think it matters too much.

- Do users need a manual correction or approval workflow?

Hands off, automated workflow.

- Should different document types be allowed to use different converters?

Not sure what this means.

## Browsing and Retrieval

Candidate capabilities:

- Browse papers by title, author, date, venue, type, tag, and processing state.
- Full-text search across converted content.
- Fast `rg`-based discovery directly from the filesystem.
- View metadata, source PDF, converted content, figures, and processing history.
- Copy stable references to a paper, section, page, or artifact.
- Filter to papers with missing sources, failures, or stale outputs.
- Produce concise library-level catalogs that agents can inspect cheaply.

We might want to build indexes as well in the library.
Indexes would still be text files of sorts, but they would include some useful stuff that helps preserve context.

Perhaps something like:

citekey: Actual title in a "title.txt"
citekey: One line abstract (AI generated using prompt workflows) in a "summary.txt"
citekey: author list
citekey: keywords?


Possible later capabilities:

- Citation and reference graph navigation.
- Saved searches or collections independent of Zotero collections.
- Duplicate detection and merge assistance.
- Optional semantic or hybrid search.
- Cross-paper comparison views.

These don't seem like they would be useful, aside from maybe duplicate detection that two papers are the same.

Semantic search should be treated as a derived feature, not assumed to be the
foundation of the library.

## Agent-Facing Use

Candidate capabilities:

- A documented, stable filesystem contract.
- Searchable Markdown and line-oriented structured data.
- CLI commands for discovery, inspection, processing, and validation.
- Stable references that an agent can include in an answer or research note.
- Clear provenance so an agent can distinguish source text from generated
  analysis.
- A generated guide explaining how an agent should navigate a library.


I think a short and sweet auto generated AGENTS.md in the library would be useful.
And some indexes inside index/

We don't need to word stuff CLI commands. Don't confuse them by adding too much. It really is just simple instructions on structure.


Possible later capabilities:

- An MCP server for structured search and retrieval.

No MCP, the library is just a bunch of text files (and images, and probably the pdfs).

- Agent-oriented context bundles for a paper or topic.

Yeah, in the form of index files, and summarizations produced using APIs (ex: using commerical APIs to create method summaries). Mainly for context reduction.

- Section-aware retrieval with source locations.

? Unclear.

- Research workflows that collect evidence across several papers.

No, there are no workflows involving actual research. Paper pipelines goal is to produce the library as an artifact that can be injected by arbitrary user workflows.

- Subagent-friendly task partitioning over a library.

Open questions:

- What should agents be allowed to modify?

Agents aren't expected to modify the libraries themselves. A sort of read only knowledge base.

- Should agent-authored notes live inside the managed library?
- What constitutes a valid citation back to local source material?

A typical workflow would be me using some .tex files, noticing that agents screwed up my background because they hallucinated paper details. Telling them to audit the .tex file vs a library.

## Analysis and Enrichment Workflows

The v1 intro and method filters demonstrate a broader possible feature family.

Candidate capabilities:

- Run a reusable prompt or extraction recipe over one or more papers.
- Define expected inputs and outputs for a recipe.
- Record the model, prompt version, settings, usage, and source artifacts.
- Retry failures without losing prior successful results.
- Keep multiple results when recipes or models change.
- Export structured results as well as human-readable Markdown.
- Distinguish deterministic processing from LLM-generated interpretation.

Possible workflows include:

- Summary and key-claim extraction.
- Method, dataset, metric, and result extraction.
- Reference parsing.
- Classification and tagging.
- Literature-review evidence collection.
- Comparison tables across selected papers.
- User-defined prompt recipes.

Open questions:

- Is a general workflow system part of the first product, or should v2 begin
  with conversion only?

We will include some stuff, particularly lets include those summary and claim extraction workflows.
They were nice. The generated MD files were helpful.

- Should recipes operate on the converted representation, the original PDF, or
  declare either as an input?

I'm not sure. I was thinking that recipies should involve some sort of template format that defines what goes to the upstream LLM provider. 

- Are workflows linear operations, composable pipelines, or eventually graphs?

For now I was thinking that they would be fairly linear.
My random thinking is that you can create templates for what happens.

ex in a template like contributions.md
```
<pdf>

Extract the key contributions in this paper.
Format them as a bulleted list.
Output just the contributions.
```

and then you could click on some papers and run this and somehow it produces a contributions.md?

- Which outputs are automatically trusted, and which require review?

Trust isn't really something we care about here.


## Jobs and Operations

Candidate capabilities:

- Queue long-running work without starting conflicting GPU processes.

The thing I'm concerned about is mainly shitty code having VRAM leaks or something.
I don't want a brittle paperpipeline. Only good way to manage this is with processes since it is forcably cleaned up.

- Display pending, running, completed, failed, cancelled, and interrupted work.

Feedback for things happening is good.

- Cancel safely and resume after an application restart.
- Retry individual papers or batches.

Decent UX ideas.

- Preserve useful logs without making terminal output the source of truth.

Logging is useful. We might permit some meta information in the library just for debugging and stuff.
Using a distinct file extensions / placement so as to be easily ignorable in rg.

- Detect stale locks and abandoned work.

Locks are interesting, but maybe not nessesary. Could be kept as future scope.

- Report runtime, converter, model, GPU, and dependency health.

Possibly future scope.

- Estimate work before starting a large batch.

Open questions:

- Must work continue when the UI or server is closed?

Decoupling the UI is decent.

- Is a single local worker sufficient, or is multi-machine execution a plausible
  future requirement?



- Which tasks may run concurrently, and how should resource limits be declared?

The thing is that you want heavy concurrency, particularly for the API calls.
But you want to be able to use context caching.
Specifically you want to have the PDF context cached for the extraction workflows to save money.

This means if you are running a bunch of things that involve a pdf, you run them sequentially for individual papers so that you get cache hits.

## Browser UI and UX

The browser UI remains desirable. The main requirement is not a particular
frontend framework; it is clear ownership of state and predictable extension.

Candidate product areas:

- Library overview and search.
- Paper detail and document reading.
- Import review.
- Conversion and workflow launch controls.
- Job queue, progress, errors, and logs.
- Configuration and runtime diagnostics.

UI quality requirements:

- A single, explicit owner for each piece of UI state.
- No duplicated behavior between full pages and injected fragments.
- Reusable components for repeated statuses and actions.
- Stable URLs for important views.
- Keyboard-accessible and responsive interactions.
- Clear empty, loading, error, offline, and interrupted states.
- Automated interaction tests for critical workflows.
- Visual regression coverage for layout-sensitive views.

The main thing i care about is that the UI needs to not bork stuff.

Open questions:

- Is the UI primarily an operational dashboard, a document reader, or a full
  research workspace?

The UI is just an operation dashboard with basic document reader support just for quick inspections.

- Should users be able to edit metadata or notes in the UI?

No need.

- Is live PDF viewing and source-to-converted-text comparison important?

Live pdf maybe, if we could save images of the pdf pages that might work too.

- Which operations need confirmation, previews, or undo?

Confirmation can be left as a future scope thing.

## Portability and Export

Candidate capabilities:

- Copy a library to another machine and retain useful content.

sure, the idea is that the library is the database. Not that we keep some seperate database state thing somewhere
for paper pipeline.

- Export selected papers as a compact agent-readable bundle.

Maybe, I was kind of hoping that maybe you could take a library and the way that it exists on disk and just delete out some papers?
The problem this might cause is indexes.

- Rebuild derived indexes and generated views.

Could be useful.

- Report external dependencies that prevent a library from being portable.

A library should always be portable if you include the whole folder.

- Support backup without copying disposable caches where possible.

Open questions:

- Must source PDFs be included for a library to count as portable?

We may or may not include the actual source PDFs in the paper folders somehow.

- Should paths inside managed data always be relative?

Paths inside should always be relative.

- Is version-control friendliness an explicit goal?

Sort of. I was thinking the library would actually be version controlled. An automatically generated gitignore probaly is useful to avoid having logs and things show up in ripgrep?

## Candidate Release Buckets

These buckets should be filled in after the feature workshop:

### First Useful Version

- To be decided.

### Likely Next

- To be decided.

### Explore Later

- To be decided.

### Explicit Non-Goals

- Commercial and multi-tenant operation for the initial product.
- Migration from the v1 database format.
- Additional non-goals to be decided.

# 3. Code Organization Plans

Detailed code structure should be designed after the first feature set is
chosen. This section records organizational requirements and candidate
boundaries without committing to directories, schemas, or frameworks.

## Organization Goals

- The domain model must not depend on a particular converter, UI, or Zotero
  export format.
- CLI and UI operations should use the same application services rather than
  reimplementing business logic.
- Processing backends should not write arbitrary files or mutate unrelated
  state directly.
- Adding a workflow should not require creating a new queue, status model, API
  family, and UI state system.
- Long-running execution, cancellation, logging, retries, and provenance should
  have shared behavior.
- Generated artifacts should be validated before becoming visible as completed
  results.
- UI code should consume explicit application contracts rather than infer state
  from filenames or placeholder text.
- Dependencies that are large, GPU-specific, or license-sensitive should be
  isolated from the core library where practical.

## Candidate Responsibility Boundaries

The feature discussion will determine whether these become real components:

- **Library domain:** papers, metadata, sources, identities, and relationships.
- **Ingestion:** Zotero and future source integrations.
- **Document processing:** conversion backends and normalized results.
- **Workflows:** reusable analysis or enrichment capabilities.
- **Job execution:** queueing, resource policy, cancellation, retries, and logs.
- **Artifact management:** persistence, provenance, validation, and discovery.
- **Indexing and retrieval:** rebuildable catalogs and search representations.
- **Application services:** user operations shared by CLI, API, and UI.
- **Interfaces:** CLI, browser API, web client, and possible future MCP server.

These names do not imply one package or service per bullet. They are a checklist
for avoiding the responsibility leaks found in v1.

## Decisions Deliberately Deferred

Do not lock these down until the relevant features are prioritized:

- Exact on-disk folder layout.
- Canonical manifest and artifact schemas.
- Stable paper identity strategy.
- Python package and module tree.
- Frontend framework and component library.
- API style and event transport.
- Plugin discovery mechanism.
- Job persistence technology.
- Whether structured document blocks are canonical or optional.
- Whether the application uses only files or also a rebuildable local database
  or index.

# 4. Development and Project Meta

## Testing

Tests are mandatory because the project is expected to be extended by agents
and the v1 UI showed how easily cross-layer behavior can regress.

The eventual test strategy should include:

- Unit tests for domain rules and parsing.
- Contract tests shared by interchangeable backends.
- Integration tests for library operations and job recovery.
- Golden-file tests over a small representative paper corpus.
- API tests for application behavior.
- Browser tests for critical user journeys.
- Visual regression tests for important layouts.
- A clean-environment smoke test using only documented setup commands.

The project should decide which test tiers are required for each kind of change
and encode those expectations in `AGENTS.md`.

## Agent Operations

The new root `AGENTS.md` should be written alongside the first concrete project
structure. It should cover:

- The purpose and high-level architecture of the project.
- Canonical sources of truth and generated-file rules.
- Exact setup, development, formatting, type-checking, and test commands.
- Safety rules for GPU and long-running processing.
- How to verify jobs from durable artifacts.
- Required checks for backend, UI, schema, and dependency changes.
- A concise definition of done.

Detailed architectural explanation should live in dedicated documentation, not
accumulate indefinitely in `AGENTS.md`.

## Decision Record

### Confirmed

- Greenfield redevelopment with no v1 database migration.
- Personal/local initial scope.
- Tests are a core requirement.
- The browser UI should be retained in some form.
- Agent-friendly local files remain central to the product idea.

### Strong Candidates

- Marker as the first document converter.
- Zotero as the first ingestion source.
- `rg`-friendly Markdown plus structured metadata for agent access.
- A shared execution model for conversion and later workflows.

### Still Open

- First-release feature boundary.
- PDF ownership and portability policy.
- Zotero import versus synchronization.
- Canonical document representation.
- Scope of general LLM workflows.
- Primary agent interface beyond the filesystem.
- Detailed backend, frontend, and storage architecture.
