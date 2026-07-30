"""Read-only job dashboard and diagnostic-log services."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

from paper_pipeline.convert.runner import ConverterSpec
from paper_pipeline.jobs.model import Job, JobKind, JobState
from paper_pipeline.jobs.recovery import InterruptedAttempt
from paper_pipeline.pages.runner import PageRendererSpec
from paper_pipeline.services.processing import retry_job
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


def list_runtime_jobs(
    runtime: LibraryRuntime,
    *,
    state: JobState | None = None,
    kind: JobKind | None = None,
) -> tuple[Job, ...]:
    """Return one library's in-memory jobs with optional contract filters."""
    return tuple(
        job
        for job in runtime.queue.list_jobs()
        if job.library_key == runtime.library_key
        and (state is None or job.state is state)
        and (kind is None or job.kind is kind)
    )


def list_interrupted_attempts(
    runtime: LibraryRuntime,
    *,
    state: JobState | None = None,
    kind: JobKind | None = None,
) -> tuple[InterruptedAttempt, ...]:
    if state not in (None, JobState.INTERRUPTED):
        return ()
    return tuple(
        attempt for attempt in runtime.interrupted_attempts if kind is None or attempt.kind is kind
    )


def job_counts(runtime: LibraryRuntime) -> dict[str, int]:
    jobs = list_runtime_jobs(runtime)
    return {state.value: sum(job.state is state for job in jobs) for state in JobState}


async def retry_selected_jobs(
    runtime: LibraryRuntime,
    job_ids: list[str],
    *,
    converter_spec: ConverterSpec,
    page_renderer_spec: PageRendererSpec | None = None,
    timeout_seconds: int,
    page_render_timeout_seconds: int | None = None,
    provider_name: str,
    model: str = "",
) -> tuple[Job, ...]:
    """Validate and retry a selected live-job batch within one library."""
    if not job_ids:
        raise ValueError("select at least one failed or cancelled job to retry")
    if len(set(job_ids)) != len(job_ids):
        raise ValueError("selected retry jobs must not contain duplicates")
    selected: list[Job] = []
    for job_id in job_ids:
        job = runtime.queue.get(job_id)
        if job is None or job.library_key != runtime.library_key:
            raise ValueError(f"selected job does not belong to this library: {job_id}")
        if job.state not in {JobState.FAILED, JobState.CANCELLED}:
            raise ValueError(f"selected job is not retryable: {job_id}")
        selected.append(job)

    replacements: list[Job] = []
    for job in selected:
        replacements.append(
            await retry_job(
                runtime,
                job.id,
                converter_spec=converter_spec,
                page_renderer_spec=page_renderer_spec,
                timeout_seconds=timeout_seconds,
                page_render_timeout_seconds=page_render_timeout_seconds,
                provider_name=provider_name,
                model=model,
            )
        )
    return tuple(replacements)


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
