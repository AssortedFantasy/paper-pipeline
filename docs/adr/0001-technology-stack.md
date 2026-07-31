# ADR-0001: Technology stack

Status: Accepted

## Context

Paper Pipeline is a local Python application. It needs a GPU-free default
development environment, a web dashboard, and optional GPU conversion.

## Decision

- Python 3.12+, packaged with uv and hatchling in a `src/` layout
- FastAPI and uvicorn for the web server
- Pydantic v2 for settings and serialized models
- Jinja2 and htmx for the server-rendered dashboard, with a small app-owned
  `EventSource` client for Server-Sent Events
- rdflib for Zotero RDF parsing
- Marker as the conversion backend, installed through the `marker` extra
- The OpenAI SDK as the built-in LLM provider client
- pytest, Playwright, ruff, and pyright for verification

The dashboard has no SPA framework, client bundler, or npm build step. Marker
and PyTorch remain outside the default installation.

The browser owns exactly one `EventSource` per loaded document. It opens the
stream on `pageshow`, closes it on `pagehide`, relies on the browser's native
reconnection behavior, and forwards job events into htmx triggers. This
lifecycle is application code rather than a generic htmx SSE extension so
normal and back/forward navigation cannot accumulate live HTTP streams.

## Consequences

Server state remains authoritative for the dashboard. Conversion code must be
importable without Marker or PyTorch installed, and default tests must not
require a GPU, network access, provider credentials, or a browser. The SSE
client carries no durable state; restoring a cached page simply creates a new
stream to the authoritative server state.
