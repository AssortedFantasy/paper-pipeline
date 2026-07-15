"""Import planning: compare an export snapshot with the current library.

Implemented by WP-2A.2. Produces an ``ImportPlan``:

- ``additions``: records whose citekey is not in the library.
- ``refreshes``: records whose citekey exists and source hash is unchanged;
  metadata will be replaced.
- ``source_replacements``: same citekey but different PDF bytes; the preview
  requires explicit acceptance and hash comparison makes old outputs stale.
- ``problems``: missing attachments, invalid citekeys, duplicate candidates
  (same DOI or near-identical title under a different citekey).

The plan is pure data — applying it is a service-layer operation.
Papers absent from the export are retained untouched.
"""
