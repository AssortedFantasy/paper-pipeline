# ADR-0004: Job execution and recovery

Status: Accepted

## Context

Conversion, recipes, imports, and maintenance can mutate the same library.
They need shared concurrency control, cancellation, and recovery without a
second durable state store.

## Decision

One process-wide job system serves all open libraries.

- Every paper mutation runs in an exclusive `(library, citekey)` lane.
- Paper-lane sessions update the runtime library catalog at the canonical
  `paper.json` write boundary (ADR-0007).
- Conversion is globally limited to one job by default.
- Local page rendering is independent of conversion, uses the same paper lane,
  and is limited to two concurrent jobs.
- Recipe cohorts run as remote-scope jobs without a library barrier while the
  provider executes. Their snapshot and finalization children use the ordinary
  exclusive paper lanes (ADR-0009).
- Library writes wait for active paper lanes and block new ones.
- Read-only validation uses a library read barrier.
- Conversion runs in a fresh child process; cancellation terminates its process
  tree.
- Page rendering also runs in a fresh child process so PDFium resources,
  cancellation, and timeouts remain bounded.

Live jobs move through `queued`, `running`, and a terminal state of
`succeeded`, `failed`, `cancelled`, or `partial`. `partial` is reserved for a
coordinator whose independently valid child outcomes were durably installed
while other children failed. A job succeeds only after its artifacts are
validated, installed, hashed, and recorded.

`paper.json` describes installed artifacts and completed attempts, not live
jobs. In-flight markers under `.pp/attempts/` are disposable recovery hints.
Markers left after a restart appear as interrupted work but never determine
artifact validity.

Job events and progress are in memory and published to the dashboard with
Server-Sent Events. Each loaded browser document owns at most one event stream
and releases it during page navigation; reconnecting or closing a browser does
not cancel work.

Remote-scope jobs use the same queue and event system but hold no paper lane or
library barrier while waiting. Their resumable provider state is disposable
operational data under `.pp`; application shutdown detaches rather than
requesting remote cancellation.

## Consequences

Callers cannot bypass paper lanes, write `paper.json` directly, or construct a
mutation session without its runtime catalog. A failed or cancelled rerun does
not invalidate an older artifact whose recorded input hash is still current.
Deleting `.pp/` removes interruption diagnostics but leaves the library valid.
