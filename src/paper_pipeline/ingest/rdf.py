"""Zotero RDF/XML parsing.

Implemented by WP-2A.1 (see PLAN.md). Parses a Zotero RDF export directory
(``.rdf`` file plus ``files/`` attachment tree) into normalized
``ImportRecord`` objects: PaperMetadata + absolute path to the exported PDF
attachment (absolute paths are fine *here*; they never enter the library).

Zotero quirks (item types, container titles, author ordering, attachment
links) stay inside this module.
"""
