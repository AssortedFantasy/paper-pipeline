# Paper Pipeline

Paper Pipeline builds portable, folder-based academic-paper libraries from
Zotero RDF exports. Each generated library contains normalized metadata,
copied source PDFs, searchable Markdown transcriptions, optional LLM-generated
analysis, and small text indexes designed for direct use with tools such as
`rg`.

The generated library is the product. Paper Pipeline is its local builder and
operations dashboard; it is not a note manager, search service, or research
workspace.

## Release status

The first useful v2 release includes core storage, RDF import, conversion
orchestration, recipes, indexes, the web API, and the operational dashboard,
covered by offline and browser tests. The
[release checklist](docs/release-checklist.md) records acceptance evidence and
the explicitly deferred external workflows.

Local GPU/OCR execution on the target laptop is prohibited by owner safety
direction. The SHA-verified three-document Marker golden suite remains
available for an approved stronger or remote machine, but is not an offline
release blocker.

## Requirements and installation

- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/)
- Chromium installed through Playwright only when running browser tests
- An NVIDIA GPU and the optional Marker extra only for local PDF conversion
- The optional LLM extra and provider credentials only for real recipes

```sh
uv sync
```

The default environment includes the application and development tools, but
not Marker, PyTorch, or the OpenAI SDK. Install optional edges deliberately:

```sh
uv sync --extra marker
uv sync --extra llm
```

## Configuration

Settings come from `PAPER_PIPELINE_*` environment variables or
`~/.paper-pipeline/.env`. Environment variables take precedence. Never put a
`.env` file or credentials inside a generated library.

```dotenv
PAPER_PIPELINE_LLM_API_KEY=...
PAPER_PIPELINE_LLM_MODEL=...
# Optional OpenAI-compatible endpoint:
PAPER_PIPELINE_LLM_BASE_URL=...
```

Conversion defaults to one local Marker child process at a time. Optional SSH
conversion settings are `PAPER_PIPELINE_REMOTE_CONVERTER_HOST`,
`PAPER_PIPELINE_REMOTE_CONVERTER_ROOT`, and
`PAPER_PIPELINE_REMOTE_CONVERTER_PYTHON`; the remote adapter currently has no
dashboard selection control.

Check the local environment without making provider or GPU calls:

```sh
uv run paper-pipeline doctor
```

## Build a library

Start the local dashboard:

```sh
uv run paper-pipeline serve
```

Then open <http://127.0.0.1:8000>. The server binds to localhost by default.

Continue in the dashboard:

1. Create a library in a selected folder, or open an existing library, from
   the library setup panel.
2. Open **Import**, choose a Zotero RDF export, and
   review additions, refreshes, source replacements, problems, and possible
   duplicates.
3. Apply the accepted plan. Source PDF replacements require explicit opt-in.
4. Select papers and launch conversion or a built-in recipe.
5. Use **Jobs** to inspect live state, diagnostic tails, cancel work, retry an
   individual failure or interrupted attempt, or retry selected failed/cancelled
   work as a batch.
6. Open a paper to inspect metadata, its source PDF, transcription, figures,
   recipe output, and provenance.
7. Validate the active library or deterministically rebuild its indexes and
   generated guidance from the maintenance panel.

The same maintenance operations are also available through the API and CLI:

```sh
uv run paper-pipeline validate "D:/Papers/My Library"
uv run paper-pipeline reindex "D:/Papers/My Library"
```

## Generated library

```text
library.json
AGENTS.md
.gitignore
indexes/
papers/<citekey>/
    paper.json
    source/<hash>.pdf
    transcription.md
    figures/
    generated/
    .pp/
.pp/
```

All stored paths are library-relative POSIX paths. `library.json`, paper
records, copied sources, transcriptions, and figures are essential content;
indexes and generated guidance are rebuildable; `.pp/` directories are
disposable operational state. The generated `.gitignore` excludes source PDFs
and `.pp/` noise, so a Git clone stays searchable but cannot reprocess papers
without restoring the source PDFs.

## Development and verification

```sh
uv run ruff format .
uv run ruff check --fix .
uv run pyright
uv run pytest
uv run python scripts/smoke.py
```

Browser, GPU, and real-provider tests are explicit workflows. See
[AGENTS.md](AGENTS.md) for the authoritative commands and safety rules.

## Project documents

| File | Purpose |
| --- | --- |
| [AGENTS.md](AGENTS.md) | Development commands, invariants, and required checks |
| [docs/adr/](docs/adr/) | Durable architecture decisions |
| [docs/release-checklist.md](docs/release-checklist.md) | Delivered requirements, deferrals, and release gates |
