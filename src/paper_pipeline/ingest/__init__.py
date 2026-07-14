"""Zotero ingestion: parse RDF exports and plan repeatable imports.

Responsibilities:

- Parse Zotero RDF exports and locate exported attachments.
- Normalize Zotero-specific data into ``ImportRecord`` objects using the
  library's ``PaperMetadata`` vocabulary.
- Compare an import snapshot with the current library and produce a
  previewable add/refresh plan.

Not responsible for writing files into the library (services + storage do
that) or for rendering previews (the UI does that).

Rules:

- Import never deletes library papers.
- Zotero-owned metadata is replaced wholesale on refresh.
- Duplicate candidates (same DOI/title, different citekey) are surfaced in
  the preview, never silently merged or duplicated.

Modules:

- ``rdf``: RDF/XML parsing -> ``ImportRecord`` list (WP-2A.1).
- ``plan``: snapshot comparison -> ``ImportPlan`` of adds/refreshes/problems (WP-2A.2).
"""
