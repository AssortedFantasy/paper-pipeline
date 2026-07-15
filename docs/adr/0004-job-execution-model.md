# ADR-0004: Job execution and interruption recovery

Status: Accepted (2026-07-14)

## Context

REFACTOR.md requires one shared job system with per-category policies, GPU
isolation via child processes, provider-cache-friendly recipe scheduling,
browser-independent execution, and conservative recovery after interruption.
Job-state persistence representation was deferred.

## Decision

1. **Jobs are in-memory objects** inside the server process. There is no job
   database. The durable record of work is:
   - each paper's `paper.json` processing record (state, timing, error), and
   - artifacts on disk (`transcription.md`, `generated/*`), plus logs under
     `.pp/`.
2. **Lifecycle**: `queued -> running -> succeeded | failed | cancelled`, plus
   `interrupted` assigned only during startup reconciliation. Before a job
   reports `succeeded`, its completion validator must confirm expected
   artifacts on disk; terminal output is never proof.
3. **Write ordering**: `paper.json` is marked `running` before work starts
   and updated with the terminal state after artifact validation. The full
   record lifecycle is `pending -> running -> succeeded | failed |
   cancelled | interrupted` (`ArtifactState` in `library/model.py`).
   Artifacts are staged in `.pp/tmp` and installed atomically before the
   record says `succeeded`.
4. **Startup reconciliation**: on opening a library, any `paper.json` record
   left in a running state with no live owner is rewritten to `interrupted`
   and surfaced in the UI as retryable. Interrupted rows in the jobs
   dashboard are synthesized from `paper.json` records at render time —
   they are not live queue jobs; retrying enqueues a fresh job. In-memory
   state is never trusted across restarts.
5. **Scheduling policies by category**:
   - `conversion`: global concurrency 1 (default); each conversion in a
     fresh child process; cancellation kills the process tree.
   - `recipe`: concurrency `llm_concurrency` across papers; strictly one job
     per paper at a time, FIFO per paper, so same-paper recipes run
     back-to-back and reuse the provider's input cache.
   - `maintenance`: reindex mutates derived files and runs exclusively
     with all other jobs; validate is read-only and requires no
     exclusivity.
6. **Events** are published on an in-process bus; the web layer forwards
   them over SSE. Browser disconnects never affect jobs.

## Consequences

- If the server process dies, active child work is orphaned/stopped and
  classified `interrupted` on next startup — accepted per REFACTOR.md.
- Resuming partial work and daemon operation remain future features and
  would amend this ADR.
