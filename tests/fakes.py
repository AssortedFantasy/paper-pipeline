"""Fake implementations of external contracts for the fast test suite.

Expanded by the work packages that need them:

- ``FakeConverter``: implements ``convert.contract.Converter``. Writes a tiny
  deterministic transcription and optional figure files into the staging
  directory. Configurable to fail, hang (for timeout tests), or crash.
- ``FakeLLMProvider``: implements ``recipes.provider.LLMProvider``. Returns
  canned text; records calls so scheduler tests can assert per-paper
  sequencing. Configurable to fail or delay.
"""
