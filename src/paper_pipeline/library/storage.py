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
