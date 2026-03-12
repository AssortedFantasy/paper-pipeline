from __future__ import annotations

import atexit
import ctypes
import os
from pathlib import Path

RUN_LOCK_FILENAME = ".paper-pipeline-run.lock"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259


def _pid_is_running_windows(pid: int) -> bool:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False

    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _pid_is_running_windows(pid)
    try:
        os.kill(pid, 0)
    except (OSError, SystemError, ValueError):
        return False
    return True


def release_run_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        return


def acquire_run_lock(workspace_root: Path) -> Path:
    from .rdf_parser import clean_text

    lock_path = workspace_root / RUN_LOCK_FILENAME
    if lock_path.exists():
        existing_pid_text = clean_text(lock_path.read_text(encoding="utf-8"))
        try:
            existing_pid = int(existing_pid_text)
        except ValueError:
            existing_pid = 0
        if pid_is_running(existing_pid):
            raise RuntimeError(
                "Another paper-pipeline run appears to still be active "
                f"(pid={existing_pid}). If that is wrong, delete {lock_path}."
            )
        release_run_lock(lock_path)

    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError(
            f"Another paper-pipeline run appears to be active. Delete {lock_path} if it is stale."
        ) from exc

    with os.fdopen(fd, "w", encoding="utf-8") as lock_file:
        lock_file.write(str(os.getpid()))

    atexit.register(release_run_lock, lock_path)
    return lock_path
