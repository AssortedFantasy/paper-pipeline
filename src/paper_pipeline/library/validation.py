"""Library validation: report actionable problems, never auto-destroy data.

Implemented by WP-1.3. Checks include:

- ``library.json`` present, readable, and a supported format version.
- Every ``papers/<citekey>/`` has a valid ``paper.json`` whose citekey
  matches its directory name.
- Citekeys match ``paths.CITEKEY_PATTERN``.
- Declared source PDFs exist (a Git clone legitimately lacks them; report
  as "not reprocessable", not corruption).
- No absolute paths in any stored record.
- Indexes reference only papers that still exist (staleness report).

Output is a structured problem report with severity and a suggested action.
Validation never deletes or rewrites paper content.
"""
