"""Enrichment recipes: compact LLM-generated Markdown artifacts per paper.

Responsibilities:

- Load and validate recipe definitions (``model``).
- Provide the LLM provider contract and adapters (``provider``).
- Resolve declared inputs (``input``) and define the provider Batch contract
  (``provider``). Durable orchestration and installation live in services.

Recipes describe work; they never implement queueing, HTTP, or storage.
Built-in recipe templates ship with the application in ``builtin/``, not
with libraries. Outputs land directly in each paper directory; provenance and
usage live in ``paper.json`` rather than output frontmatter (ADR-0003).
Credentials never appear in
outputs, provenance, or logs.
"""
