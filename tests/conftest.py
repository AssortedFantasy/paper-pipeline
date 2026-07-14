"""Shared fixtures for the Paper Pipeline test suite.

Rules (see AGENTS.md):

- The default suite is fast: no GPU, no paid APIs, no network, no real
  browser. Tests needing those carry the ``gpu``/``llm``/``browser`` markers.
- Tests operate on temporary libraries; never touch a real user library.
- Fakes for the converter and LLM provider live in ``tests/fakes.py``.
"""

from pathlib import Path

import pytest


@pytest.fixture
def library_root(tmp_path: Path) -> Path:
    """An empty directory suitable for creating a test library in."""
    root = tmp_path / "library"
    root.mkdir()
    return root
