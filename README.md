# Paper Pipeline (v2)

Paper Pipeline is a local library builder for academic papers. It consumes a
Zotero RDF export and produces a portable, folder-based library that humans and
LLM agents can browse and search with ordinary filesystem tools (`rg`, `ls`,
plain file reads). The generated library is the product; this application is
the tool that builds and maintains it.

**Status: greenfield rebuild in progress.** The previous implementation lives
under `v1/` as reference material only.

## Documents

| File | Purpose |
| --- | --- |
| [REFACTOR.md](REFACTOR.md) | Product requirements and architectural direction (source of truth for *what*) |
| [PLAN.md](PLAN.md) | Implementation plan broken into agent-executable work packages |
| [AGENTS.md](AGENTS.md) | Operational rules, commands, and required checks for agents |
| [docs/adr/](docs/adr/) | Architecture decision records (source of truth for *how*) |

## Quick start (development)

```sh
uv sync                 # core + dev tools; no GPU, no LLM SDKs
uv run pytest           # fast default suite
uv run paper-pipeline   # CLI entry point
```

See [AGENTS.md](AGENTS.md) for the full command reference.
