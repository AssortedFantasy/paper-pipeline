"""Library model and storage: the versioned library format and its invariants.

This is the innermost package. It must not import from any other
``paper_pipeline`` subpackage, and it must never import FastAPI, Marker,
RDF tooling, or LLM SDKs.

Responsibilities (see REFACTOR.md "Library Model and Storage"):

- The versioned library format and its invariants.
- Paper identity (citekeys), metadata, and source ownership.
- Relative path handling: no absolute paths are ever written into a library.
- Safe reads and atomic writes (write temp + rename; never partial files).
- Classification of essential, derived, and disposable files.
- Validation and repair reporting.

Modules:

- ``paths``: the canonical on-disk layout — the single source of truth for
  every filename and directory name inside a library.
- ``model``: serialized data types (``LibraryInfo``, ``PaperMetadata``,
  ``PaperRecord``, processing records).
- ``storage``: create/open a library, enumerate papers, read/write paper
  records, atomic artifact installation.
- ``validation``: library validator producing actionable problem reports.
"""
