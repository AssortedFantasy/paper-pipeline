"""Import planning: compare an export snapshot with the current library.

Implemented by WP-2A.2. Produces an ``ImportPlan``:

- ``additions``: records whose citekey is not in the library.
- ``refreshes``: records whose citekey exists; metadata will be replaced.
- ``problems``: missing attachments, invalid citekeys, duplicate candidates
  (same DOI or near-identical title under a different citekey).

The plan is pure data — applying it is a service-layer operation.
Papers absent from the export are retained untouched.
"""
