# Improvements plan

Implicit goal for every item: keep all docs (README, AGENTS.md, ADRs, library
docs) up to date and cohesive with the change.

Guiding principles for the library format (items 1–3):

- Minimize the number of hops for an LLM agent to reach the information it
  wants.
- Minimize wasted tokens once it gets there.

## How to work this plan

- **Items 1–3 change the library format.** Do them together as one work
  package: one ADR-0002 update, one format-version bump, one migration story.
  Prefer in-place migration of existing libraries (e.g. via a `migrate`
  step or as part of `validate`/`reindex`); regenerating from scratch is a
  fallback, not the plan.
- Items 4–7 are independent of the format work and of each other.
- Item 3 also changes the provider contract, so it needs its own ADR review
  per AGENTS.md.

## 1. Flatten `generated/` into the paper folder (format change)

The entire point of Paper Pipeline is that `papers/<citekey>/` **is**
generated content. An extra `generated/` subfolder adds hierarchy where none
is needed and costs agents an extra hop.

Direction:

- Recipe outputs land directly in the paper folder:
  `papers/ashWarmStartingNeuralNetwork2020/contributions.md`, next to
  `transcription.md`.
- Hierarchy is reserved only for things agents should ignore or large sets
  of similar files: pdf page images, run logs, figure images, source files.
- `source/` stays exactly as it is (deliberate decision — it is essential
  library content and must not move into `.pp/`, which must always be safe
  to delete).
- Guard against recipe output names colliding with reserved file names
  (`paper.json`, `transcription.md`, etc.).
- `paper.json` remains the authority on which files are regenerable, since
  flat layout removes the "everything under `generated/` is derived" cue.

Done when: outputs are flat, ADR-0002 and README/library-AGENTS.md reflect
it, `validate`/`reindex` and tests updated, existing libraries migrate.

## 2. Remove frontmatter from generated files (same work package as 1)

Generated files currently start with a YAML frontmatter block
(`generated_by`, `recipe`, `recipe_version`, `provider`, `model`, `input`,
`input_sha256`, `created`). This information is worth tracking — including
future API-spend data — but agents reading `contributions.md` do not need
it; it is pure wasted tokens.

Provenance is already dual-written to `paper.json` (`RecipeRecord`), so
nothing is lost: stop writing the frontmatter block, keep the JSON records.
Do not invent a new sidecar file. Generated markdown should contain only the
recipe output.

## 3. LLM usage and spend tracking (contract + format change)

Today there is zero indication of LLM spend anywhere — not on the dashboard,
not in logs, not in the schema. There is also no visibility into whether
prompt caching is working.

Direction:

- Extend the provider contract (`ProviderResult`) to carry token usage:
  prompt tokens, cached tokens, completion tokens, and computed cost.
- Persist usage per recipe run in `paper.json` (`RecipeRecord`) — this is
  part of the item 1–3 format bump.
- Surface spend in the dashboard (see item 4: a spend column in the papers
  table is a natural fit) and in run logs.
- Caching: **instrument first, conclude second.** Record `cached_tokens`
  from real API responses before deciding caching is broken. Note that
  OpenAI now charges for cache writes on the 5.6-series models — this is a
  new cost factor that must be managed and reflected in cost computation,
  not assumed free. The per-paper-lane sequential recipe execution exists
  specifically so the provider can reuse input context; verify it actually
  produces cache hits and that the write cost is worth it.

Done when: every recipe run records tokens + cost in `paper.json`, totals
are visible in the UI, and cache hit rate is observable from recorded data.

## 4. UI is too sparse — make it dense

Hard-assume a desktop viewport and optimize for information density,
particularly for conversion workflows. Not prescribing a pixel-level design —
the agent should produce a plan — but the main ideas are:

- Smaller fonts, low padding, dense tables.
- Papers table columns: paper (title), authors, citekey (its own column),
  year, converted, summary status — and optionally LLM spend (item 3).
- Move subtitles and help/explanatory text into hover text (tooltips)
  instead of taking up layout space.
- The existing tab structure is fine; this is not a tab redesign.
- Sortable columns (title, citekey, year) — fold sorting into this table
  rework so it isn't built twice.

Record the density/desktop-viewport principle briefly in AGENTS.md (or an
existing design doc) so future UI work follows it.

## 5. UI / UX workflow fixes

The select → run → observe flow is janky. Concrete requirements:

- Select all / unselect all for the papers table.
- Recipes are currently run one at a time via a dropdown: to run all four
  recipes on a paper you must dropdown → run → dropdown → run, four times.
  Replace this with a checklist of all recipes defined in the library:
  pick which recipes to run, pick which papers to run them on, and run the
  full cartesian product as one action.
- While recipes/conversions run, show live per-paper status and progress.
  Source this from the existing job system in `paper_pipeline.jobs` —
  AGENTS.md forbids a second queue or status store.

## 6. Make the LLM SDK a required dependency

Installation and running is confusing. The library is useless if neither
marker nor the LLM SDK is installed, so remove the weirdness: drop the
`llm` extra and make the SDK a core dependency.

- Marker stays optional (it drags in GPU/torch; default `uv sync` must stay
  GPU-free per AGENTS.md).
- The default test suite still runs offline with no credentials — the fake
  provider remains the default, and the `llm` pytest marker still gates
  real-provider tests.
- Update AGENTS.md commands/dependency-profile language, README install
  docs, `doctor`, `scripts/smoke.py`, and commit the updated `uv.lock`.

## 7. Document and verify remote SSH conversion

Remote conversion is **already implemented** (ADR-0005, `RemoteConverter`,
`remote_converter_host` config) — but there is no user-facing documentation,
so as a user I couldn't tell it was supported or how to use it.

- Write setup docs: what to configure, what the remote host needs installed
  (target: Ubuntu 24 with an RTX 3090, reachable as `ssh noesis`), and make
  explicit that the dashboard keeps running locally — only conversion work
  ships over SSH.
- Then actually test it end-to-end against `noesis` and fix what breaks.
  Treat this as an untested feature until proven.

Done when: a user can follow the docs from zero to a remote conversion.

