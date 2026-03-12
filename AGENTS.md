# paper-pipeline Agent State

Operational guide for AI agents working in this repo.

## What This Repo Is

Turns a Zotero RDF export plus attached PDFs into a local, file-based paper database with Nougat transcriptions and a web dashboard.

Main outputs: `papers/<citekey>/meta.md`, `papers/<citekey>/transcribed.md`, `papers/<citekey>/status.json`, `papers/<citekey>/nougat_raw/`.

Entry points: `paper-pipeline` (CLI), `paper-gui` (web UI).

## Commands

- Setup: `uv sync`
- Health check: `uv run paper-pipeline doctor`
- Build metadata: `uv run paper-pipeline build`
- Run one paper: `uv run paper-pipeline run-nougat --citekey <citekey>`
- Dry run: `uv run paper-pipeline run-nougat --dry-run --citekey <citekey>`
- Launch GUI: `uv run paper-gui`

## Hard Rules

- Never start more than one `paper-pipeline run-nougat` or `nougat` process at a time.
- Do not reuse a terminal that may have an active Nougat run.
- Do not probe with extra terminal commands while a Nougat run may be in progress.
- Verify success from disk artifacts, not terminal output.
- If `.paper-pipeline-run.lock` exists and no Nougat process is alive, delete the stale lock.

## Safe Verification

Check these files to confirm a transcription succeeded:

- `papers/<citekey>/transcribed.md`
- `papers/<citekey>/nougat_raw/`
- `papers/<citekey>/status.json`

## Documentation

- `README.md` — setup and usage for humans.
- `ARCHITECTURE.md` — design notes and future directions.
- `AGENTS.md` — this file; operational rules for agents.
