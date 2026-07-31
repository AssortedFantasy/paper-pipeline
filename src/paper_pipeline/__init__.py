"""Paper Pipeline: build portable, agent-searchable paper libraries from Zotero exports.

Dependency direction (enforced by review; see AGENTS.md):

    HTMX templates -> web routes -> services
        -> {library, ingest, convert, pages, recipes, jobs, indexes}
            -> external tools (Marker, PDFium, RDF, LLM SDKs, filesystem)

``library`` is the innermost package. It must not import from any other
``paper_pipeline`` subpackage.
"""

__version__ = "2.0.0a0"
