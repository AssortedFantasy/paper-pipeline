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
- ``Library.list_papers() -> list[PaperRecord]``
- ``Library.read_paper(citekey: str) -> PaperRecord``
- ``Library.write_paper(record: PaperRecord) -> None``          (atomic)
- ``Library.install_artifact(citekey, relative_dest, staged_src) -> None``  (atomic)
- ``Library.operational_dir() -> Path``                          (.pp/, created on demand)
"""
