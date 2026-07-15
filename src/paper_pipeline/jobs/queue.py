"""Job queue and scheduler.

Implemented by WP-2D.1/2D.2. The queue exposes resource-aware entry points so
callers cannot forget locking:

- ``enqueue_paper``: one exclusive lane per (library, citekey), shared by
  conversion, a sequential recipe batch, and import apply.
- ``enqueue_library_write``: waits for paper lanes and blocks new ones while
  mutating maintenance runs.
- ``enqueue_library_read``: read-only validation without a write barrier.
- Conversion also observes global concurrency 1; recipe batches observe
  ``llm_concurrency`` across papers.

Supports enqueue, cancel (current and queued), retry, and completion
validation before a job may report SUCCEEDED. Survives browser disconnects:
nothing here depends on a connected client.
"""
