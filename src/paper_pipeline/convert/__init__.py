"""Document conversion: PDF -> Markdown through a replaceable converter contract.

Responsibilities (see REFACTOR.md "Document Conversion"):

- A small converter contract expressed in product terms (``contract``).
- Launching converters in fresh child processes so GPU memory and backend
  failures are contained at paper boundaries (``runner``).
- The Marker adapter, keeping Marker-specific flags and output quirks out of
  the rest of the application (``marker``).

Marker (and torch) must only be imported inside the child process entry
point, never by application code. ``import paper_pipeline.convert`` must
succeed without the ``marker`` extra installed.
"""
