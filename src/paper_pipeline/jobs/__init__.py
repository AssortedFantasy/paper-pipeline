"""Job execution: the single shared operational layer for all heavy work.

Responsibilities (ADR-0004):

- Queueing and task state transitions (``model``).
- Scheduling policies per task category (``queue``): GPU conversion is
  serialized (concurrency 1); remote recipe jobs run concurrently across
  papers but strictly sequentially per paper (provider cache reuse).
- Process lifecycle, cancellation, retries, and completion validation.
- Publishing progress events to interested interfaces (``events``).

Conversion and recipes must NOT grow separate job systems. Jobs live in
memory; durable truth is each paper's ``paper.json`` plus artifacts on disk.
On startup, reconciliation marks papers whose records say "running" as
interrupted (ADR-0004).
"""
