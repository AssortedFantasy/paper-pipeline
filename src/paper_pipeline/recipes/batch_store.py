"""Atomic disposable storage for remotely executing recipe runs."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from paper_pipeline.library.paths import OPERATIONAL_DIR, RECIPE_RUNS_DIR
from paper_pipeline.recipes.batch_model import RecipeRunManifest, RecipeRunState

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_DISPOSABLE_FILES = (
    "errors.jsonl",
    "output.jsonl",
    "requests.jsonl",
)
_DISPOSABLE_DIRECTORIES = ("collected", "snapshots")


class RecipeRunStore:
    """Persist resumable provider state without becoming artifact truth."""

    def __init__(self, library_root: Path) -> None:
        self.library_root = library_root.resolve()
        self.operational_root = self.library_root / OPERATIONAL_DIR
        self.runs_root = self.operational_root / RECIPE_RUNS_DIR
        self.temp_root = self.operational_root / "tmp"

    def create(self, manifest: RecipeRunManifest, state: RecipeRunState) -> Path:
        if manifest.run_id != state.run_id:
            raise ValueError("recipe run manifest and state IDs do not match")
        run_dir = self.initialize(manifest.run_id)
        self.write_manifest(manifest)
        self.write_state(state)
        return run_dir

    def initialize(self, run_id: str) -> Path:
        run_dir = self.run_dir(run_id)
        if run_dir.exists():
            raise FileExistsError(f"recipe run already exists: {run_id}")
        run_dir.mkdir(parents=True)
        (run_dir / "collected").mkdir()
        (run_dir / "snapshots").mkdir()
        return run_dir

    def run_dir(self, run_id: str) -> Path:
        self._validate_id(run_id)
        return self.runs_root / run_id

    def write_manifest(self, manifest: RecipeRunManifest) -> None:
        destination = self.run_dir(manifest.run_id) / "manifest.json"
        if destination.exists():
            raise FileExistsError(f"recipe run manifest already exists: {manifest.run_id}")
        self._atomic_write(destination, manifest.model_dump(mode="json"))

    def read_manifest(self, run_id: str) -> RecipeRunManifest:
        return RecipeRunManifest.model_validate_json(
            (self.run_dir(run_id) / "manifest.json").read_text(encoding="utf-8")
        )

    def write_state(self, state: RecipeRunState) -> None:
        self._atomic_write(
            self.run_dir(state.run_id) / "state.json",
            state.model_dump(mode="json"),
        )

    def read_state(self, run_id: str) -> RecipeRunState:
        return RecipeRunState.model_validate_json(
            (self.run_dir(run_id) / "state.json").read_text(encoding="utf-8")
        )

    def list_run_ids(self) -> list[str]:
        if not self.runs_root.is_dir():
            return []
        return [
            entry.name
            for entry in sorted(self.runs_root.iterdir())
            if entry.is_dir()
            and _SAFE_ID.fullmatch(entry.name)
            and (entry / "manifest.json").is_file()
            and (entry / "state.json").is_file()
        ]

    def path(self, run_id: str, *parts: str) -> Path:
        path = self.run_dir(run_id).joinpath(*parts)
        resolved_parent = path.parent.resolve()
        if not resolved_parent.is_relative_to(self.run_dir(run_id).resolve()):
            raise ValueError("recipe run path escapes its run directory")
        return path

    def write_json(self, run_id: str, filename: str, value: object) -> None:
        self._atomic_write(self.path(run_id, filename), value)

    def prune_payload(self, run_id: str) -> int:
        """Remove large, reconstructable files after a run no longer needs recovery."""
        removed_bytes = 0
        for filename in _DISPOSABLE_FILES:
            path = self.path(run_id, filename)
            if path.is_file() and not path.is_symlink():
                removed_bytes += path.stat().st_size
                path.unlink()
            elif path.is_symlink():
                path.unlink()
        for dirname in _DISPOSABLE_DIRECTORIES:
            path = self.path(run_id, dirname)
            if path.is_symlink():
                path.unlink()
                continue
            if not path.is_dir():
                continue
            removed_bytes += sum(
                item.stat().st_size
                for item in path.rglob("*")
                if item.is_file() and not item.is_symlink()
            )
            shutil.rmtree(path)
        return removed_bytes

    def discard(self, run_id: str) -> None:
        """Discard an unreadable operational run without affecting library artifacts."""
        run_dir = self.run_dir(run_id)
        if run_dir.is_symlink():
            run_dir.unlink()
        elif run_dir.is_dir():
            shutil.rmtree(run_dir)

    def _atomic_write(self, destination: Path, value: object) -> None:
        self.temp_root.mkdir(parents=True, exist_ok=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="recipe-run-",
            suffix=".tmp",
            dir=self.temp_root,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                json.dump(value, output, indent=2, sort_keys=True, ensure_ascii=False)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _validate_id(run_id: str) -> None:
        if not _SAFE_ID.fullmatch(run_id):
            raise ValueError(f"unsafe recipe run ID: {run_id!r}")
