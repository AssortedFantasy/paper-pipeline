"""Index generation: concise, rebuildable, agent-oriented text files.

Responsibilities:

- Read canonical paper content (``paper.json`` files and generated outputs).
- Produce the ``indexes/`` files, the library's root ``AGENTS.md``, and its
  ``.gitignore``.
- Rebuild deterministically: same library contents -> byte-identical output.
- Reconcile the index directory to the supported set, dropping obsolete files
  and entries for manually removed paper directories.

Indexes are derived content and never become the canonical paper registry.

Indexes: ``titles.md``, ``authors.md``, ``years.md``, ``venues.md``, and
``summaries.md`` — one line per paper, ``<citekey>: <value>``, sorted by
citekey. Missing values use explicit placeholders so every paper appears in
every index.
"""
