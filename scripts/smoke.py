"""Run the core smoke test from a temporary checkout with a fresh environment."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="paper-pipeline-smoke-") as temporary:
        checkout = Path(temporary) / "paper-pipeline"
        checkout.mkdir()
        _copy_git_visible_files(ROOT, checkout)
        _run(["uv", "sync"], checkout)
        _run(
            ["uv", "run", "pytest", "-m", "slow", "tests/test_smoke.py"],
            checkout,
        )
    return 0


def _copy_git_visible_files(source: Path, destination: Path) -> None:
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=source,
        check=True,
        capture_output=True,
    ).stdout
    for raw_path in listed.split(b"\0"):
        if not raw_path:
            continue
        relative = Path(raw_path.decode("utf-8"))
        incoming = source / relative
        if not incoming.is_file():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(incoming, target)


def _run(command: list[str], cwd: Path) -> None:
    print(f"smoke: {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=cwd, check=True, timeout=300)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f"smoke failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
