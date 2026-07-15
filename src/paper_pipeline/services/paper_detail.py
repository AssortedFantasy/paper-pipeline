"""Read-only, path-safe paper detail and media services."""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from paper_pipeline.jobs.model import Job, JobKind, JobState
from paper_pipeline.jobs.queue import CancellationToken
from paper_pipeline.library.model import PaperRecord, RecipeRecord
from paper_pipeline.library.paths import FIGURES_DIR, PAPERS_DIR
from paper_pipeline.library.storage import conversion_is_fresh, sha256_file
from paper_pipeline.services.runtime import LibraryRuntime, LibrarySession

_IMAGE_TYPES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}


@dataclass(frozen=True)
class GeneratedArtifact:
    name: str
    content: str
    record: RecipeRecord


@dataclass(frozen=True)
class PaperDetailData:
    record: PaperRecord
    conversion_status: str
    transcription: str | None
    generated: tuple[GeneratedArtifact, ...] = ()
    figures: tuple[str, ...] = ()
    source_available: bool = False
    artifact_warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class MediaArtifact:
    content: bytes
    media_type: str
    filename: str


async def get_paper_detail(runtime: LibraryRuntime, citekey: str) -> PaperDetailData:
    """Read one paper and only its validated, in-library display artifacts."""
    results: list[PaperDetailData] = []

    async def read(session: LibrarySession, job: Job, token: CancellationToken) -> None:
        del job, token
        record = session.read_paper(citekey)
        root = session.root_path(f"{PAPERS_DIR}/{citekey}").parents[1]
        warnings: list[str] = []

        transcription = _validated_text(
            root,
            f"{PAPERS_DIR}/{citekey}/transcription.md",
            record.conversion.transcription_sha256,
        )
        if record.conversion.transcription_sha256 is not None and transcription is None:
            warnings.append("The recorded transcription is missing or no longer matches its hash.")
        if transcription is None:
            if (
                record.conversion.last_attempt
                and record.conversion.last_attempt.state.value == "failed"
            ):
                conversion_status = "Not converted · latest attempt failed"
            else:
                conversion_status = "Not converted"
        elif not conversion_is_fresh(record):
            conversion_status = "Stale"
        elif (
            record.conversion.last_attempt
            and record.conversion.last_attempt.state.value == "failed"
        ):
            conversion_status = "Ready · latest rerun failed"
        else:
            conversion_status = "Ready"

        generated: list[GeneratedArtifact] = []
        for name, recipe in sorted(record.recipes.items()):
            if recipe.output_artifact is None:
                continue
            content = _validated_text(root, recipe.output_artifact, recipe.output_sha256)
            if content is None:
                if recipe.output_sha256 is not None:
                    warnings.append(f"Generated output {name!r} is missing or has changed.")
                continue
            generated.append(GeneratedArtifact(name=name, content=content, record=recipe))

        figures = _figure_names(root, citekey) if transcription is not None else ()
        source_available = False
        if record.source_pdf is not None:
            source = _safe_file(root, record.source_pdf, (PAPERS_DIR, citekey, "source"))
            source_available = (
                source is not None
                and record.source_sha256 is not None
                and sha256_file(source) == record.source_sha256
            )
        results.append(
            PaperDetailData(
                record=record,
                conversion_status=conversion_status,
                transcription=transcription,
                generated=tuple(generated),
                figures=figures,
                source_available=source_available,
                artifact_warnings=tuple(warnings),
            )
        )

    job = await runtime.enqueue_library_read(JobKind.MAINTENANCE, "paper:detail-view", read)
    completed = await runtime.queue.wait(job.id)
    _require_read(completed, citekey)
    return results[0]


async def get_source_pdf(runtime: LibraryRuntime, citekey: str) -> MediaArtifact:
    """Return a hash-validated source PDF without exposing a filesystem path."""
    artifacts: list[MediaArtifact] = []

    async def read(session: LibrarySession, job: Job, token: CancellationToken) -> None:
        del job, token
        record = session.read_paper(citekey)
        if record.source_pdf is None or record.source_sha256 is None:
            raise FileNotFoundError("paper has no source PDF")
        root = session.root_path(f"{PAPERS_DIR}/{citekey}").parents[1]
        source = _safe_file(root, record.source_pdf, (PAPERS_DIR, citekey, "source"))
        if source is None or sha256_file(source) != record.source_sha256:
            raise FileNotFoundError("paper source PDF is missing or invalid")
        artifacts.append(
            MediaArtifact(
                content=source.read_bytes(),
                media_type="application/pdf",
                filename="source.pdf",
            )
        )

    job = await runtime.enqueue_library_read(JobKind.MAINTENANCE, "paper:source", read)
    completed = await runtime.queue.wait(job.id)
    _require_read(completed, citekey)
    return artifacts[0]


async def get_figure(runtime: LibraryRuntime, citekey: str, figure: str) -> MediaArtifact:
    """Return one safe image from this paper's figures directory."""
    relative = PurePosixPath(figure)
    if (
        not figure
        or "\\" in figure
        or relative.is_absolute()
        or ".." in relative.parts
        or str(relative) != figure
        or relative.suffix.casefold() not in _IMAGE_TYPES
    ):
        raise FileNotFoundError("invalid figure path")
    artifacts: list[MediaArtifact] = []

    async def read(session: LibrarySession, job: Job, token: CancellationToken) -> None:
        del job, token
        session.read_paper(citekey)
        root = session.root_path(f"{PAPERS_DIR}/{citekey}").parents[1]
        stored = f"{PAPERS_DIR}/{citekey}/{FIGURES_DIR}/{figure}"
        path = _safe_file(root, stored, (PAPERS_DIR, citekey, FIGURES_DIR))
        if path is None:
            raise FileNotFoundError("figure is missing")
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        artifacts.append(MediaArtifact(path.read_bytes(), media_type, path.name))

    job = await runtime.enqueue_library_read(JobKind.MAINTENANCE, "paper:figure", read)
    completed = await runtime.queue.wait(job.id)
    _require_read(completed, citekey)
    return artifacts[0]


def _validated_text(root: Path, stored: str, expected_hash: str | None) -> str | None:
    if expected_hash is None:
        return None
    parts = PurePosixPath(stored).parts
    if len(parts) < 3:
        return None
    path = _safe_file(root, stored, parts[:3])
    if path is None or sha256_file(path) != expected_hash:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeError:
        return None


def _figure_names(root: Path, citekey: str) -> tuple[str, ...]:
    figures_root = root / PAPERS_DIR / citekey / FIGURES_DIR
    if not _safe_components(root, figures_root) or not figures_root.is_dir():
        return ()
    names: list[str] = []
    for path in sorted(figures_root.rglob("*")):
        if path.is_symlink():
            return ()
        if path.is_file() and path.suffix.casefold() in _IMAGE_TYPES:
            names.append(path.relative_to(figures_root).as_posix())
    return tuple(names)


def _safe_file(root: Path, stored: str, expected_prefix: tuple[str, ...]) -> Path | None:
    if not stored or "\\" in stored:
        return None
    relative = PurePosixPath(stored)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or str(relative) != stored
        or relative.parts[: len(expected_prefix)] != expected_prefix
    ):
        return None
    candidate = root.joinpath(*relative.parts)
    if not _safe_components(root, candidate) or not candidate.is_file():
        return None
    return candidate


def _safe_components(root: Path, candidate: Path) -> bool:
    root = root.resolve()
    try:
        relative = candidate.absolute().relative_to(root)
    except ValueError:
        return False
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return False
    return True


def _require_read(job: Job, citekey: str) -> None:
    if job.state is JobState.SUCCEEDED:
        return
    error = job.error or f"could not read paper {citekey!r}"
    if "FileNotFoundError" in error or "ValueError" in error:
        raise FileNotFoundError(error)
    raise RuntimeError(error)
