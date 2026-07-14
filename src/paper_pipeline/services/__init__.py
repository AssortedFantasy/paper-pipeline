"""Application services: the single implementation of user-level operations.

Both the web API and the CLI call these functions; business rules live here
and nowhere above (see AGENTS.md architecture rules).

Operations (implemented by WP-3.1..3.3):

- ``create_library`` / ``open_library`` / ``validate_library``
- ``preview_import(library, export_path) -> ImportPlan``
- ``apply_import(library, plan) -> ImportReport``   (copies PDFs, writes records)
- ``queue_conversion(library, citekeys) -> [Job]``
- ``queue_recipe(library, recipe_name, citekeys) -> [Job]``
- ``cancel_job`` / ``retry_job``
- ``rebuild_indexes(library)``

Services orchestrate; they do not parse RDF, spawn Marker, or format HTTP
responses. Every mutation goes through library storage's atomic writes.
"""
