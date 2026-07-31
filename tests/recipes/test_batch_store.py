from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from paper_pipeline.recipes.batch_model import RecipeRunPhase, RecipeRunState
from paper_pipeline.recipes.batch_store import RecipeRunStore


def test_state_write_retries_a_transient_destination_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RecipeRunStore(tmp_path / "library")
    store.initialize("run-1")
    state = RecipeRunState(
        run_id="run-1",
        phase=RecipeRunPhase.PLANNING,
        updated_at=datetime.now(UTC),
    )
    store.write_state(state)
    real_replace = os.replace
    attempts = 0
    delays: list[float] = []

    def transient_lock(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError(5, "destination is briefly locked", destination)
        real_replace(source, destination)

    monkeypatch.setattr("paper_pipeline.recipes.batch_store.os.replace", transient_lock)
    monkeypatch.setattr("paper_pipeline.recipes.batch_store.time.sleep", delays.append)
    state.phase = RecipeRunPhase.IN_PROGRESS

    store.write_state(state)

    assert attempts == 3
    assert delays == [0.01, 0.02]
    assert store.read_state("run-1").phase is RecipeRunPhase.IN_PROGRESS
