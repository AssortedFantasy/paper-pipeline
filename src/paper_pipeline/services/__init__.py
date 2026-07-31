"""Application services: the single implementation of user-level operations.

Both the web API and the CLI call these functions; business rules live here
and nowhere above (see AGENTS.md architecture rules).

Operations:

- ``create_library`` / ``open_library`` / ``validate_library``
- ``preview_import(runtime, export_path) -> ImportPlan``
- ``apply_import(runtime, plan) -> ImportReport``
- ``queue_conversion(runtime, citekeys) -> [Job]``
- ``queue_recipes(runtime, recipe_names, citekeys) -> [Job]`` creates the
  fewest durable remote Batch coordinators allowed by provider limits.
- ``cancel_job`` / ``retry_job``
- ``rebuild_indexes(library)``
- ``refresh_catalog(runtime)``

Services orchestrate; they do not parse RDF, spawn Marker, or format HTTP
responses. A process-wide runtime registry owns each open library and its
queue and prepared read catalog. Every paper mutation receives a scoped
``PaperSession`` inside the mandatory paper lane, then uses library storage's
atomic writes. Successful paper-record writes update the catalog; explicit
refresh reconciles changes made outside the process.
"""
