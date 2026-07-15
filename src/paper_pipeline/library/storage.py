"""Library storage: create/open libraries, read/write papers, atomic installs.

Implemented by WP-1.1/1.2. Key invariants:

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
- ``Library.install_artifact(...)`` / ``install_conversion_bundle(...)``   (atomic)
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
import uuid
from collections.abc import Iterator
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


def recipe_is_fresh(record: PaperRecord, recipe_name: str) -> bool:
    """Return whether recipe provenance matches its current declared input.

    Input artifact paths may be library-relative or paper-relative. Their basename
    identifies the two input kinds supported by the version-1 contract.
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
        destination_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(self.root, destination_dir / PAPER_FILE, record)


def create_library(root: Path, name: str = "") -> Library:
    """Create a format-version 1 library, refusing any non-empty directory."""
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
            f"{FORMAT_VERSION}; migrate it with a compatible Paper Pipeline version"
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
    for recipe_name, recipe in record.recipes.items():
        yield f"recipes.{recipe_name}.input_artifact", recipe.input_artifact
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


def _atomic_write_json(root: Path, destination: Path, model: LibraryInfo | PaperRecord) -> None:
    temp_dir = root / OPERATIONAL_DIR / "tmp"
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
