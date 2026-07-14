"""Recipe execution: resolve input, call provider, validate, add provenance.

Implemented by WP-2C.3. For one (paper, recipe) pair:

1. Resolve the declared input (transcription text or source PDF); fail
   fast with a clear error if it is missing.
2. Call the configured provider.
3. Validate the result (non-empty, plausibly Markdown).
4. Prepend YAML front matter provenance (recipe name/version, provider,
   model, created timestamp, input artifact — never credentials).
5. Return the staged output for atomic installation into ``generated/``.

Scheduling (same-paper sequencing for provider cache reuse) is the job
layer's responsibility, not this module's.
"""
