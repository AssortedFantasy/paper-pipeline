"""Library storage: create/open libraries, read/write papers, atomic installs.

Key invariants:

- All writes are atomic: write to a temp file/dir under the library's
  ``.pp/tmp`` area, validate, then rename into place.
- An output is not complete until it has been validated and atomically
  installed in its final location.
- No absolute paths are ever serialized.
- Restarting the application recovers truth by reading ``paper.json`` files
  and artifacts on disk, never a second database.

Planned surface (signatures may gain parameters, not lose them):

- ``create_library(root: Path, name: str = "") -> Library``
- ``open_library(root: Path) -> Library``
- ``Library.list_papers() -> tuple[list[PaperRecord], list[str]]``
  (valid papers, plus problem descriptions for invalid paper dirs —
  reported, never raised)
- ``Library.read_paper(citekey: str) -> PaperRecord``
- ``Library.write_paper(record: PaperRecord) -> None``          (atomic infrastructure API)
- transcription and page bundle installers                       (atomic)
- ``Library.operational_dir() -> Path``                          (.pp/, created on demand)

Application services never call raw mutation methods directly. A
``LibraryRuntime`` supplies a citekey-scoped ``PaperSession`` only while the
shared job queue holds that paper's lane (ADR-0004).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath, PureWindowsPath

from pydantic import ValidationError

from paper_pipeline.library.model import LibraryInfo, PaperRecord
from paper_pipeline.library.paths import (
    CITEKEY_PATTERN,
    FORMAT_VERSION,
    INDEXES_DIR,
    LIBRARY_FILE,
    OPERATIONAL_DIR,
    PAPER_FILE,
    PAPERS_DIR,
    paper_dir,
)

_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
ArtifactValidator = Callable[[Path], None]


def validate_citekey(citekey: str) -> None:
    """Reject citekeys that cannot be used as portable directory names."""
    if re.fullmatch(CITEKEY_PATTERN, citekey) is None:
        raise ValueError(f"Invalid citekey {citekey!r}: expected pattern {CITEKEY_PATTERN}")
    # Windows reserves these names even when an extension is present.
    if citekey.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise ValueError(f"Invalid citekey {citekey!r}: it is a Windows-reserved name")


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Hash a source file incrementally so staging need not load it into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def conversion_is_fresh(record: PaperRecord) -> bool:
    """Return whether installed conversion provenance matches the current source."""
    current_hash = record.source_sha256
    return current_hash is not None and record.conversion.source_sha256 == current_hash


def page_render_is_fresh(record: PaperRecord) -> bool:
    """Return whether installed page images were rendered from the current source."""
    current_hash = record.source_sha256
    return (
        current_hash is not None
        and record.pages.source_sha256 == current_hash
        and record.pages.page_count > 0
        and len(record.pages.artifacts) == record.pages.page_count
    )


def recipe_is_fresh(record: PaperRecord, recipe_name: str) -> bool:
    """Return whether recipe provenance matches its current declared input.

    Input artifact paths may be library-relative or paper-relative. Their basename
    identifies the two input kinds supported by the current contract.
    """
    recipe = record.recipes.get(recipe_name)
    if recipe is None or recipe.input_artifact is None or recipe.input_sha256 is None:
        return False

    input_path = PurePosixPath(recipe.input_artifact)
    if input_path.name == "transcription.md":
        current_hash = record.conversion.transcription_sha256
    elif "source" in input_path.parts:
        current_hash = record.source_sha256
    else:
        return False
    return current_hash is not None and recipe.input_sha256 == current_hash


class Library:
    """A folder-backed paper library."""

    def __init__(self, root: Path, info: LibraryInfo) -> None:
        self.root = root.resolve()
        self.info = info

    def operational_dir(self) -> Path:
        """Return the disposable operational directory, creating it on demand."""
        directory = self.root / OPERATIONAL_DIR
        _ensure_safe_managed_path(self.root, directory)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def list_papers(self) -> tuple[list[PaperRecord], list[str]]:
        """Read valid paper directories and report malformed ones as problems."""
        records: list[PaperRecord] = []
        problems: list[str] = []
        papers_root = self.root / PAPERS_DIR
        if not papers_root.is_dir():
            return records, [f"Missing papers directory: {papers_root}"]

        for entry in sorted(papers_root.iterdir(), key=lambda path: path.name.casefold()):
            if not entry.is_dir():
                problems.append(f"Unexpected entry in papers directory: {entry.name}")
                continue
            try:
                validate_citekey(entry.name)
                records.append(self.read_paper(entry.name))
            except (OSError, ValueError, ValidationError, json.JSONDecodeError) as error:
                problems.append(f"Invalid paper directory {entry.name!r}: {error}")
        return records, problems

    def read_paper(self, citekey: str) -> PaperRecord:
        """Read and validate one paper record."""
        validate_citekey(citekey)
        record_path = paper_dir(self.root, citekey) / PAPER_FILE
        _ensure_safe_managed_path(self.root, record_path)
        try:
            record = PaperRecord.model_validate_json(record_path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise FileNotFoundError(f"Paper {citekey!r} has no {PAPER_FILE}") from error
        _validate_paper_record(record, expected_citekey=citekey)
        return record

    def write_paper(self, record: PaperRecord) -> None:
        """Atomically write a paper record (infrastructure API)."""
        citekey = record.metadata.citekey
        validate_citekey(citekey)
        _validate_paper_record(record, expected_citekey=citekey)
        destination_dir = paper_dir(self.root, citekey)
        _ensure_safe_managed_path(self.root, destination_dir)
        destination_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(self.root, destination_dir / PAPER_FILE, record)

    def stage_dir(self) -> Path:
        """Create a fresh staging directory on the library filesystem."""
        temp_root = self.operational_dir() / "tmp"
        _ensure_safe_managed_path(self.root, temp_root)
        temp_root.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=f"{os.getpid()}-", dir=temp_root))

    def install_artifact(
        self,
        staged_path: Path,
        destination: str | PurePosixPath,
        *,
        validate: ArtifactValidator | None = None,
    ) -> str:
        """Validate and atomically install one staged file, returning its SHA-256."""
        staged_path = staged_path.resolve()
        _require_staged_file(self.root, staged_path)
        relative_destination = _coerce_relative_destination(destination)
        destination_path = self.root.joinpath(*relative_destination.parts)
        _ensure_safe_managed_path(self.root, destination_path)
        if validate is not None:
            validate(staged_path)
        artifact_hash = sha256_file(staged_path)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged_path, destination_path)
        return artifact_hash

    def install_transcription_bundle(
        self,
        citekey: str,
        staging_dir: Path,
        *,
        validate: ArtifactValidator | None = None,
    ) -> dict[str, str]:
        """Install a staged transcription and its referenced figures as one bundle.

        Validation and hashing finish before installed content is touched. Ordinary
        exceptions roll back to the previous bundle. A process-ending interruption
        may leave new bytes with old metadata, which the returned/stored hashes make
        detectable during validation.
        """
        validate_citekey(citekey)
        staging_dir = staging_dir.resolve()
        _require_staging_dir(self.root, staging_dir)
        _validate_conversion_stage(staging_dir)
        if validate is not None:
            validate(staging_dir)

        transcription = staging_dir / "transcription.md"
        figures = staging_dir / "figures"
        paper_root = paper_dir(self.root, citekey)
        _ensure_safe_managed_path(self.root, paper_root)
        if not paper_root.is_dir():
            raise FileNotFoundError(f"Paper {citekey!r} does not exist")

        hashes = {
            f"papers/{citekey}/transcription.md": sha256_file(transcription),
            **{
                f"papers/{citekey}/figures/{figure.relative_to(figures).as_posix()}": sha256_file(
                    figure
                )
                for figure in sorted(figures.rglob("*"))
                if figure.is_file()
            },
        }
        backup = self.stage_dir()
        installed: list[Path] = []
        backed_up: list[tuple[Path, Path]] = []
        targets = [
            (transcription, paper_root / "transcription.md", backup / "transcription.md"),
            (figures if figures.is_dir() else None, paper_root / "figures", backup / "figures"),
        ]
        for _source, destination, _prior in targets:
            _ensure_safe_managed_path(self.root, destination)
        try:
            for source, destination, prior in targets:
                if destination.exists():
                    os.replace(destination, prior)
                    backed_up.append((prior, destination))
                if source is not None:
                    os.replace(source, destination)
                    installed.append(destination)
        except BaseException:
            for path in reversed(installed):
                _remove_path(path)
            for prior, destination in reversed(backed_up):
                if prior.exists():
                    os.replace(prior, destination)
            raise
        finally:
            shutil.rmtree(backup, ignore_errors=True)
        return hashes

    def install_pages_bundle(self, citekey: str, staging_dir: Path) -> dict[str, str]:
        """Atomically replace only one paper's independently rendered page images."""
        validate_citekey(citekey)
        staging_dir = staging_dir.resolve()
        _require_staging_dir(self.root, staging_dir)
        _validate_pages_stage(staging_dir)

        pages = staging_dir / "pages"
        paper_root = paper_dir(self.root, citekey)
        _ensure_safe_managed_path(self.root, paper_root)
        if not paper_root.is_dir():
            raise FileNotFoundError(f"Paper {citekey!r} does not exist")
        destination = paper_root / "pages"
        _ensure_safe_managed_path(self.root, destination)
        hashes = {
            f"papers/{citekey}/pages/{page.relative_to(pages).as_posix()}": sha256_file(page)
            for page in sorted(pages.rglob("*"))
            if page.is_file()
        }

        backup_root = self.stage_dir()
        backup = backup_root / "pages"
        installed = False
        backed_up = False
        try:
            if destination.exists():
                os.replace(destination, backup)
                backed_up = True
            os.replace(pages, destination)
            installed = True
        except BaseException:
            if installed:
                _remove_path(destination)
            if backed_up and backup.exists():
                os.replace(backup, destination)
            raise
        finally:
            shutil.rmtree(backup_root, ignore_errors=True)
        return hashes


def create_library(root: Path, name: str = "") -> Library:
    """Create a current-format library, refusing any non-empty directory."""
    root = root.resolve()
    if root.exists() and not root.is_dir():
        raise ValueError(f"Library root is not a directory: {root}")
    if root.exists() and any(root.iterdir()):
        raise ValueError(f"Refusing to create a library in non-empty directory: {root}")

    root.mkdir(parents=True, exist_ok=True)
    (root / PAPERS_DIR).mkdir()
    (root / INDEXES_DIR).mkdir()
    (root / OPERATIONAL_DIR / "tmp").mkdir(parents=True)
    info = LibraryInfo(format_version=FORMAT_VERSION, created_at=_utc_now(), name=name)
    _atomic_write_json(root, root / LIBRARY_FILE, info)
    return Library(root, info)


def open_library(root: Path) -> Library:
    """Open a library after validating its serialized format version."""
    root = root.resolve()
    info_path = root / LIBRARY_FILE
    try:
        raw = json.loads(info_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"Not a Paper Pipeline library: missing {info_path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid {LIBRARY_FILE}: {error}") from error

    version = raw.get("format_version") if isinstance(raw, dict) else None
    if not isinstance(version, int):
        raise ValueError(f"Invalid {LIBRARY_FILE}: format_version must be an integer")
    if version > FORMAT_VERSION:
        raise ValueError(
            f"Library format version {version} is newer than supported version "
            f"{FORMAT_VERSION}; upgrade Paper Pipeline to open this library"
        )
    if version < FORMAT_VERSION:
        raise ValueError(
            f"Library format version {version} is older than supported version "
            f"{FORMAT_VERSION}; rebuild the library from its Zotero RDF export"
        )
    try:
        info = LibraryInfo.model_validate(raw)
    except ValidationError as error:
        raise ValueError(f"Invalid {LIBRARY_FILE}: {error}") from error
    return Library(root, info)


def _utc_now():
    from datetime import UTC, datetime

    return datetime.now(UTC)


def _record_paths(record: PaperRecord) -> Iterator[tuple[str, str | None]]:
    yield "source_pdf", record.source_pdf
    yield (
        "conversion.last_attempt.log_path",
        (record.conversion.last_attempt.log_path if record.conversion.last_attempt else None),
    )
    yield (
        "pages.last_attempt.log_path",
        (record.pages.last_attempt.log_path if record.pages.last_attempt else None),
    )
    for page_path in record.pages.artifacts:
        yield f"pages.artifacts.{page_path}", page_path
    for recipe_name, recipe in record.recipes.items():
        yield f"recipes.{recipe_name}.input_artifact", recipe.input_artifact
        yield f"recipes.{recipe_name}.output_artifact", recipe.output_artifact
        yield (
            f"recipes.{recipe_name}.last_attempt.log_path",
            (recipe.last_attempt.log_path if recipe.last_attempt else None),
        )


def _validate_paper_record(record: PaperRecord, *, expected_citekey: str) -> None:
    if record.format_version != FORMAT_VERSION:
        raise ValueError(
            f"Paper {expected_citekey!r} has format version {record.format_version}; "
            f"supported version is {FORMAT_VERSION}"
        )
    if record.metadata.citekey != expected_citekey:
        raise ValueError(
            f"Paper citekey mismatch: directory is {expected_citekey!r}, "
            f"metadata says {record.metadata.citekey!r}"
        )
    for field, value in _record_paths(record):
        if value is not None:
            _validate_relative_posix_path(value, field=field)
    if record.source_pdf is not None:
        source_parts = PurePosixPath(record.source_pdf).parts
        if source_parts[:3] != ("papers", expected_citekey, "source") or len(source_parts) != 4:
            raise ValueError(
                "source_pdf must be a library-relative file inside this paper's source directory"
            )
    for page_path, digest in record.pages.artifacts.items():
        parts = PurePosixPath(page_path).parts
        if (
            parts[:3] != ("papers", expected_citekey, "pages")
            or len(parts) != 4
            or not parts[-1].casefold().endswith(".png")
        ):
            raise ValueError(
                "pages.artifacts keys must be library-relative PNG files inside "
                "this paper's pages directory"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("pages.artifacts values must be lowercase SHA-256 hashes")
    if record.pages.page_count != len(record.pages.artifacts):
        raise ValueError("pages.page_count must match the number of declared page artifacts")
    expected_input_root = ("papers", expected_citekey)
    for recipe_name, recipe in record.recipes.items():
        if recipe.input_artifact is not None:
            parts = PurePosixPath(recipe.input_artifact).parts
            if parts[:2] != expected_input_root or (
                parts[2:] != ("transcription.md",)
                and not (parts[2:3] == ("source",) and len(parts) > 3)
            ):
                raise ValueError(
                    f"recipes.{recipe_name}.input_artifact must reference this paper's "
                    "library-relative transcription or source path"
                )
        if recipe.output_artifact is not None:
            output_parts = PurePosixPath(recipe.output_artifact).parts
            if (
                output_parts[:2] != ("papers", expected_citekey)
                or len(output_parts) != 3
                or not output_parts[-1].endswith(".md")
            ):
                raise ValueError(
                    f"recipes.{recipe_name}.output_artifact must be a library-relative "
                    "Markdown file in this paper's directory"
                )
            from paper_pipeline.library.paths import RESERVED_PAPER_NAMES

            if output_parts[-1].casefold() in {name.casefold() for name in RESERVED_PAPER_NAMES}:
                raise ValueError(
                    f"recipes.{recipe_name}.output_artifact collides with a reserved paper filename"
                )


def _validate_relative_posix_path(value: str, *, field: str) -> None:
    if not value or "\\" in value:
        raise ValueError(f"{field} must be a non-empty POSIX path relative to the library")
    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or ".." in posix_path.parts
    ):
        raise ValueError(f"{field} must be a POSIX path relative to the library: {value!r}")
    if str(posix_path) != value or value == ".":
        raise ValueError(f"{field} is not a normalized POSIX path: {value!r}")


def _coerce_relative_destination(value: str | PurePosixPath) -> PurePosixPath:
    text = str(value)
    _validate_relative_posix_path(text, field="artifact destination")
    return PurePosixPath(text)


def _ensure_safe_managed_path(root: Path, path: Path) -> None:
    """Reject managed paths redirected through symlinks or outside *root*."""
    root = root.resolve()
    try:
        relative = path.absolute().relative_to(root)
    except ValueError as error:
        raise ValueError(f"managed path escapes the library root: {path}") from error
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"managed library path must not contain symlinks: {current}")
        if current.exists() and not current.resolve().is_relative_to(root):
            raise ValueError(f"managed path escapes the library root: {current}")


def _require_staging_dir(root: Path, path: Path) -> None:
    _ensure_safe_managed_path(root, root / OPERATIONAL_DIR / "tmp")
    temp_root = (root / OPERATIONAL_DIR / "tmp").resolve()
    if not path.is_dir() or not path.is_relative_to(temp_root) or path == temp_root:
        raise ValueError("staging directory must be a child of the library .pp/tmp directory")


def _require_staged_file(root: Path, path: Path) -> None:
    _ensure_safe_managed_path(root, root / OPERATIONAL_DIR / "tmp")
    temp_root = (root / OPERATIONAL_DIR / "tmp").resolve()
    if not path.is_file() or not path.is_relative_to(temp_root):
        raise ValueError("artifact must be a staged file inside the library .pp/tmp directory")


def _validate_conversion_stage(staging_dir: Path) -> None:
    allowed = {"transcription.md", "figures"}
    unexpected = sorted(path.name for path in staging_dir.iterdir() if path.name not in allowed)
    if unexpected:
        raise ValueError(f"conversion staging directory contains undeclared entries: {unexpected}")
    transcription = staging_dir / "transcription.md"
    if (
        transcription.is_symlink()
        or not transcription.is_file()
        or transcription.stat().st_size == 0
    ):
        raise ValueError("transcription bundle requires a non-empty transcription.md")
    figures = staging_dir / "figures"
    if figures.exists():
        if figures.is_symlink() or not figures.is_dir():
            raise ValueError("transcription bundle figures entry must be a real directory")
        if any(path.is_symlink() for path in figures.rglob("*")):
            raise ValueError("transcription bundle figures must not contain symlinks")


def _validate_pages_stage(staging_dir: Path) -> None:
    unexpected = sorted(path.name for path in staging_dir.iterdir() if path.name != "pages")
    if unexpected:
        raise ValueError(f"page-render staging directory contains undeclared entries: {unexpected}")
    pages = staging_dir / "pages"
    if pages.is_symlink() or not pages.is_dir():
        raise ValueError("page-render bundle requires a real pages directory")
    if any(path.is_symlink() for path in pages.rglob("*")):
        raise ValueError("page-render bundle must not contain symlinks")
    page_files = sorted(path for path in pages.rglob("*") if path.is_file())
    if not page_files or any(
        path.suffix.casefold() != ".png" or path.stat().st_size == 0 for path in page_files
    ):
        raise ValueError("page-render bundle must contain only non-empty PNG page images")
    expected_names = {f"page{index}.png" for index in range(1, len(page_files) + 1)}
    if {path.name for path in page_files} != expected_names or any(
        path.parent != pages for path in page_files
    ):
        raise ValueError("page-render bundle must contain one flat contiguous pageN.png sequence")


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _atomic_write_json(root: Path, destination: Path, model: LibraryInfo | PaperRecord) -> None:
    temp_dir = root / OPERATIONAL_DIR / "tmp"
    _ensure_safe_managed_path(root, temp_dir)
    _ensure_safe_managed_path(root, destination)
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / f"{uuid.uuid4().hex}.json"
    payload = model.model_dump_json(indent=2) + "\n"
    try:
        with temp_path.open("x", encoding="utf-8", newline="\n") as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temp_path, destination)
    except BaseException:
        # Temp files are disposable, but eager cleanup keeps normal failures tidy.
        temp_path.unlink(missing_ok=True)
        raise
