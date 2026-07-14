"""Enrichment recipes: compact LLM-generated Markdown artifacts per paper.

Responsibilities:

- Load and validate recipe definitions (``model``).
- Provide the LLM provider contract and adapters (``provider``).
- Resolve declared inputs, call the provider, validate output, and record
  provenance (``runner``).

Recipes describe work; they never implement queueing, HTTP, or storage.
Built-in recipe templates ship with the application in ``builtin/``, not
with libraries. Outputs land in each paper's ``generated/`` directory with
YAML front matter provenance (ADR-0003). Credentials never appear in
outputs, provenance, or logs.
"""
