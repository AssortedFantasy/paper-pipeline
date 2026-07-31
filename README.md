# Paper Pipeline

Paper Pipeline builds portable paper libraries from Zotero RDF exports. A
library contains bibliographic metadata, copied source PDFs, Markdown
transcriptions, optional LLM-generated analysis, and small text indexes.

Libraries are ordinary folders that can be used by agents for literature search.

## Features

- Repeatable Zotero RDF import with preview and explicit source replacement
- PDF-to-Markdown conversion with Marker
- Independent local rendering of low-resolution PDF page images
- Built-in summary, contribution, introduction, and method recipes
- Rebuildable indexes for titles, authors, years, venues, and summaries
- A local dashboard for importing, processing, inspecting, and retrying work
- One server-rendered HTMX interaction surface (plus SSE job updates)
- Local or SSH-based conversion

## Installation

Paper Pipeline requires Python 3.12 or newer and
[`uv`](https://docs.astral.sh/uv/).

```sh
uv sync
```

Marker and its GPU dependencies are optional:

```sh
uv sync --extra marker
```

Install Chromium to run browser tests:

```sh
uv run playwright install chromium
```

## Configuration

Settings use `PAPER_PIPELINE_*` environment variables or
`~/.paper-pipeline/.env`. Environment variables take precedence.

To run LLM recipes:

```dotenv
PAPER_PIPELINE_LLM_API_KEY=...
PAPER_PIPELINE_LLM_MODEL=...
```

`PAPER_PIPELINE_LLM_BASE_URL` may be set for an OpenAI-compatible endpoint.

Optional SSH conversion uses these user-level settings:

```dotenv
PAPER_PIPELINE_REMOTE_CONVERTER_HOST=gpu-host
PAPER_PIPELINE_REMOTE_CONVERTER_ROOT=/tmp/paper-pipeline
PAPER_PIPELINE_REMOTE_CONVERTER_PYTHON=python3
```

## Usage

Start the dashboard:

```sh
uv run paper-pipeline serve
```

Open <http://127.0.0.1:8000>, create or open a library, and import a Zotero RDF
export. The dashboard can then run conversion and recipe jobs, inspect their
outputs, and retry failures.

The CLI also provides maintenance commands:

```sh
uv run paper-pipeline doctor
uv run paper-pipeline validate "D:/Papers/My Library"
uv run paper-pipeline reindex "D:/Papers/My Library"
```

## Library layout

```text
library.json
AGENTS.md
indexes/
papers/<citekey>/
    paper.json
    source/<hash>.pdf
    transcription.md
    figures/
    pages/
    <recipe-output>.md
```

Paths stored in library metadata are relative POSIX paths. Generated
`AGENTS.md` and index files describe how to browse the library.

## Development

```sh
uv run ruff format .
uv run ruff check --fix .
uv run pyright
uv run pytest
uv run pytest -m browser
```

See [AGENTS.md](AGENTS.md) for repository rules and optional test suites.
[AGENTS.md](AGENTS.md), the code, and tests describe the current architecture.
[ADRs](docs/adr/) record decision context and are not an independent source of
current behavior.
