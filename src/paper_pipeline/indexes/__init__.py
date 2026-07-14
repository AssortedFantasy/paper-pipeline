"""Index generation: concise, rebuildable, agent-oriented text files.

Responsibilities (see REFACTOR.md "Agent-Oriented Indexes and Retrieval"):

- Read canonical paper content (``paper.json`` files and generated outputs).
- Produce the ``indexes/`` files, the library's root ``AGENTS.md``, and its
  ``.gitignore``.
- Rebuild deterministically: same library contents -> byte-identical output.
- Detect and drop stale entries for manually removed paper directories.

Indexes are derived content and never become the canonical paper registry.

First indexes (WP-2E.1): ``titles.md``, ``authors.md``, ``summaries.md``,
``status.md`` — one line per paper, ``<citekey>: <value>``, sorted by citekey.
"""
