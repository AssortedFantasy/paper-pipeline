"""Job execution: the single shared operational layer for all heavy work.

Responsibilities (ADR-0004):

- Queueing and task state transitions (``model``).
- Mandatory paper lanes (``queue``): conversion, recipe batches, and import
  apply are mutually exclusive per paper. GPU conversion is also globally
  serialized; recipe batches run concurrently across papers.
- Process lifecycle, cancellation, retries, and completion validation.
- Publishing progress events to interested interfaces (``events``).

Conversion and recipes must NOT grow separate job systems. Jobs live in
memory; durable truth is each paper's ``paper.json`` plus artifacts on disk.
Disposable ``.pp/attempts`` markers let startup surface interrupted work
without rewriting valid artifact records (ADR-0004).
"""
