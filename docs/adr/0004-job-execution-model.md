# ADR-0004: Job execution, paper lanes, and interruption recovery

Status: Accepted, amended (2026-07-15)

## Context

Paper Pipeline requires one shared job system, isolated conversion processes,
provider-cache-friendly recipe scheduling, browser-independent execution, and
conservative recovery. Atomic file writes alone do not prevent two operations
from reading the same `paper.json` and overwriting each other's changes.
Recording `running` in the durable artifact record also makes a failed rerun
hide the fact that an older valid artifact still exists.

## Decision

1. **One application queue, one runtime per open library.** A process-wide
   registry owns the shared queue/event bus and returns a `LibraryRuntime`
   keyed by resolved library root. Opening the same root again returns the same
   runtime. Each runtime owns its raw storage and provider instances but uses
   the application queue, so conversion concurrency remains global across
   libraries and `(library root, citekey)` lanes cannot be duplicated. Closing
   a view does not close the runtime or its jobs.

2. **All paper mutations use a mandatory paper lane.** The queue exposes
   `enqueue_paper(...)` for conversion, recipe batches, and each record applied
   by an import. It acquires the lane internally before invoking the supplied
   worker; callers cannot receive or bypass the underlying lock. Direct
   application writes to `paper.json` are not a service API. This gives every
   `(library root, citekey)` a single read-modify-write sequence across job
   categories.

3. **Recipe batching is the cache-reuse unit.** One recipe request may contain
   one or more recipe names for each paper. The paper-lane worker runs that
   paper's recipes sequentially using the same provider instance/input cache.
   Different paper lanes may run concurrently up to `llm_concurrency`.
   Conversion remains globally limited to one child process by default.

4. **Library barriers are explicit queue operations.** Mutating maintenance
   such as reindex uses `enqueue_library_write(...)`, which waits for active
   paper lanes and prevents new ones until it finishes. Read-only validation
   uses `enqueue_library_read(...)`. Import apply is decomposed into paper-lane
   jobs and therefore cannot race conversion or recipes for the same paper.

5. **Durable records describe completed truth, not live jobs.** `paper.json`
   records the installed artifact provenance and the latest completed attempt.
   It is not rewritten to `running` or `interrupted`. A failed or cancelled
   rerun updates `last_attempt` but does not erase the provenance of an older
   valid artifact.

6. **In-flight markers are disposable recovery hints.** Immediately before
   external work starts, the queue atomically writes
   `.pp/attempts/<job-id>.json` with the library-relative target, operation,
   and start time. On normal completion it first installs and records the
   terminal result, then removes the marker. Startup scans remaining markers
   and presents them as interrupted, retryable work without rewriting
   `paper.json`. If a terminal record already contains the marker's attempt ID,
   the stale marker is simply removed. Deleting `.pp/` loses diagnostics and
   the interrupted label, but never artifact truth or the ability to retry.

7. **Artifacts prove their inputs.** Source PDFs, transcriptions, and recipe
   outputs carry SHA-256 values in `paper.json`. A conversion is current only
   when its recorded source hash equals the current source hash. A recipe is
   current only when its recorded input hash equals the selected PDF or
   transcription hash. Source replacement therefore makes dependent outputs
   stale by comparison; no caller must remember to invalidate several flags.

8. **Lifecycle and completion.** Live jobs use
   `queued -> running -> succeeded | failed | cancelled`; `interrupted` is a
   synthesized recovery view, never a live state. A job reaches `succeeded`
   only after its expected artifacts have been validated, atomically installed,
   hashed, and recorded. Retry creates a new job addressed by the interrupted
   marker or by `(citekey, operation)`, not by pretending the old job is live.

9. **Events are in-process.** The queue retains only each live job's latest
   progress message and publishes state/progress over SSE. The papers table and
   Jobs dashboard render that shared state; they do not maintain a second
   status store. Browser disconnects never affect jobs.

## Consequences

- The lane and barrier APIs, rather than developer convention, enforce the
  locking rules. Tests must attempt cross-category races, including conversion
  versus recipe and import versus conversion.
- Recovery does not perform a library-wide rewrite at startup and never
  downgrades a valid older artifact merely because a later attempt crashed.
- `.pp/attempts/` is not a second database: it is safe to delete and is never
  used to decide whether an installed artifact is valid.
- Source hashes are modest additional metadata that remove scattered manual
  invalidation logic.
- The contracts are versioned and may be amended as implementation feedback
  arrives; serialized changes receive a format-version review and compatibility
  tests rather than being treated as permanently frozen.
