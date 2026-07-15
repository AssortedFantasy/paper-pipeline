"""Read-only job dashboard and diagnostic-log services."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

from paper_pipeline.jobs.model import Job
from paper_pipeline.jobs.recovery import InterruptedAttempt
from paper_pipeline.services.runtime import LibraryRuntime


@dataclass(frozen=True)
class JobDashboard:
    active: tuple[Job, ...]
    terminal: tuple[Job, ...]
    interrupted: tuple[InterruptedAttempt, ...]


@dataclass(frozen=True)
class LogTail:
    job_id: str
    path: str
    text: str
    truncated: bool


def job_dashboard(runtime: LibraryRuntime) -> JobDashboard:
    """Return jobs for one library grouped for operational presentation."""
    jobs = [job for job in runtime.queue.list_jobs() if job.library_key == runtime.library_key]
    jobs.reverse()
    return JobDashboard(
        active=tuple(job for job in jobs if not job.state.is_terminal),
        terminal=tuple(job for job in jobs if job.state.is_terminal),
        interrupted=tuple(reversed(runtime.interrupted_attempts)),
    )


def read_log_tail(
    runtime: LibraryRuntime,
    job_id: str,
    *,
    line_limit: int = 80,
    byte_limit: int = 128 * 1024,
) -> LogTail:
    """Read a bounded diagnostic tail through a nonexclusive library session."""
    if line_limit < 1 or byte_limit < 1:
        raise ValueError("log tail limits must be positive")
    job = runtime.queue.get(job_id)
    if job is None or job.library_key != runtime.library_key:
        raise KeyError(f"unknown job: {job_id}")
    if job.log_path is None:
        raise ValueError("this job has no diagnostic log")
    relative = PurePosixPath(job.log_path)
    windows = PureWindowsPath(job.log_path)
    if (
        "\\" in job.log_path
        or relative.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or ".." in relative.parts
        or str(relative) != job.log_path
    ):
        raise ValueError("diagnostic log path must be library-relative POSIX")
    path = runtime.root.joinpath(*relative.parts)
    root = runtime.root.resolve()
    current = root
    for part in path.relative_to(runtime.root).parts:
        current /= part
        if current.is_symlink():
            raise ValueError("diagnostic log path must not contain symlinks")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError("diagnostic log is outside the library or is not a file")
    size = resolved.stat().st_size
    with resolved.open("rb") as source:
        if size > byte_limit:
            source.seek(size - byte_limit)
        raw = source.read(byte_limit)
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return LogTail(
        job_id=job.id,
        path=job.log_path,
        text="\n".join(lines[-line_limit:]),
        truncated=size > byte_limit or len(lines) > line_limit,
    )
