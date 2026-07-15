"""Job queue and scheduler.

Implemented by WP-2D.1/2D.2. Enforces the scheduling policies:

- ``CONVERSION``: global concurrency 1 (configurable, default 1).
- ``RECIPE``: up to ``llm_concurrency`` jobs across papers, but never more
  than one job per paper at a time — same-paper recipes run back-to-back to
  reuse the provider's input cache.
- ``MAINTENANCE``: reindex mutates derived files and runs exclusively with
  all other jobs; validate is read-only and requires no exclusivity.

Supports enqueue, cancel (current and queued), retry, and completion
validation before a job may report SUCCEEDED. Survives browser disconnects:
nothing here depends on a connected client.
"""
