# ADR-0001: Technology stack

Status: Accepted (2026-07-14)

## Context

The product is personal local software, extended primarily by agents, with hard
requirements on testability, a fast GPU-free dev loop, and a UI whose state
has one explicit owner.

## Decision

- **Python >= 3.12**, packaged with **uv** + **hatchling**, `src/` layout.
- **FastAPI + uvicorn** for the web server (proven in v1, well known to
  agents).
- **Pydantic v2** for all serialized formats (`library.json`, `paper.json`)
  and settings (`pydantic-settings`).
- **Server-rendered Jinja2 + htmx + Server-Sent Events** for the UI. No SPA
  framework, no client bundler, no npm build step. Rationale: the UI is an
  operations dashboard; server-rendered fragments make "server state is the
  only truth" structural rather than disciplinary, and remove an entire
  toolchain agents could misconfigure. SSE handles one-way job progress;
  WebSockets are not needed.
- **rdflib** for Zotero RDF/XML parsing.
- **Marker** (`marker-pdf`) as the first conversion backend, behind the
  versioned converter contract, installed via the optional `marker` extra.
- **OpenAI SDK** (a required core dependency; OpenAI-compatible endpoints) as
  the first LLM provider,
  behind the versioned provider contract, via the optional `llm` extra.
- **pytest** (+ pytest-asyncio, httpx) for tests; **Playwright** for browser
  and visual-regression tests; **ruff** for format+lint; **pyright**
  (standard mode) for type checking.
- Optional extras keep GPU/provider dependencies out of the default
  `uv sync`; the fast test suite excludes `gpu`/`llm`/`browser` markers.

## Consequences

- No npm toolchain: htmx is vendored as a single static file.
- If the dashboard ever needs rich client interactivity beyond htmx's reach,
  that is a new ADR, not an incremental drift into inline JS.
- Marker's CUDA/torch pinning on Windows is handled inside the `marker`
  extra and verified by the representative converter corpus; core development
  never needs it.
