# ADR-0004: Job execution and recovery

Status: Accepted

## Context

Conversion, recipes, imports, and maintenance can mutate the same library.
They need shared concurrency control, cancellation, and recovery without a
second durable state store.

## Decision

One process-wide job system serves all open libraries.

- Every paper mutation runs in an exclusive `(library, citekey)` lane.
- Conversion is globally limited to one job by default.
- Recipes run concurrently across papers and sequentially within a paper lane.
- Library writes wait for active paper lanes and block new ones.
- Read-only validation uses a library read barrier.
- Conversion runs in a fresh child process; cancellation terminates its process
  tree.

Live jobs move through `queued`, `running`, and a terminal state of
`succeeded`, `failed`, or `cancelled`. A job succeeds only after its artifacts
are validated, installed, hashed, and recorded.

`paper.json` describes installed artifacts and completed attempts, not live
jobs. In-flight markers under `.pp/attempts/` are disposable recovery hints.
Markers left after a restart appear as interrupted work but never determine
artifact validity.

Job events and progress are in memory and published to the dashboard with
Server-Sent Events. Closing a browser does not cancel work.

## Consequences

Callers cannot bypass paper lanes or write `paper.json` directly. A failed or
cancelled rerun does not invalidate an older artifact whose recorded input hash
is still current. Deleting `.pp/` removes interruption diagnostics but leaves
the library valid.
