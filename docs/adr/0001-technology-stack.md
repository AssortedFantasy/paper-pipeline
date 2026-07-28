# ADR-0001: Technology stack

Status: Accepted

## Context

Paper Pipeline is a local Python application. It needs a GPU-free default
development environment, a web dashboard, and optional GPU conversion.

## Decision

- Python 3.12+, packaged with uv and hatchling in a `src/` layout
- FastAPI and uvicorn for the web server
- Pydantic v2 for settings and serialized models
- Jinja2, htmx, and Server-Sent Events for the dashboard
- rdflib for Zotero RDF parsing
- Marker as the conversion backend, installed through the `marker` extra
- The OpenAI SDK as the built-in LLM provider client
- pytest, Playwright, ruff, and pyright for verification

The dashboard has no SPA framework, client bundler, or npm build step. Marker
and PyTorch remain outside the default installation.

## Consequences

Server state remains authoritative for the dashboard. Conversion code must be
importable without Marker or PyTorch installed, and default tests must not
require a GPU, network access, provider credentials, or a browser.
