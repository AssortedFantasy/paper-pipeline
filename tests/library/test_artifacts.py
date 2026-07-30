"""Atomic staging and artifact installation tests."""

import os
from pathlib import Path, PurePosixPath

import pytest

from paper_pipeline.library.model import PaperMetadata, PaperRecord
from paper_pipeline.library.paths import FORMAT_VERSION
from paper_pipeline.library.storage import create_library, sha256_file


def _record() -> PaperRecord:
    return PaperRecord(
        format_version=FORMAT_VERSION,
        metadata=PaperMetadata(citekey="Smith2024", title="A paper"),
    )


def _paper_library(root: Path):
    library = create_library(root)
    library.write_paper(_record())
    return library


def _files_under(root: Path) -> dict[Path, bytes]:
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_stage_dirs_are_fresh_and_confined_to_operational_storage(library_root: Path) -> None:
    library = create_library(library_root)

    first = library.stage_dir()
    second = library.stage_dir()

    assert first != second
    assert first.is_dir() and second.is_dir()
    assert first.is_relative_to(library.operational_dir())
    assert second.is_relative_to(library.operational_dir())


def test_install_single_artifact_validates_hashes_and_replaces(library_root: Path) -> None:
    library = _paper_library(library_root)
    first_stage = library.stage_dir()
    first = first_stage / "summary.md"
    first.write_text("first", encoding="utf-8")

    digest = library.install_artifact(first, "papers/Smith2024/summary.md")

    destination = library_root / "papers" / "Smith2024" / "summary.md"
    assert destination.read_text(encoding="utf-8") == "first"
    assert digest == sha256_file(destination)

    replacement_stage = library.stage_dir()
    replacement = replacement_stage / "summary.md"
    replacement.write_text("replacement", encoding="utf-8")
    validated = False

    def validate(staged: Path) -> None:
        nonlocal validated
        validated = True
        assert staged.read_text(encoding="utf-8") == "replacement"
        assert destination.read_text(encoding="utf-8") == "first"

    replacement_digest = library.install_artifact(
        replacement,
        "papers/Smith2024/summary.md",
        validate=validate,
    )

    assert validated
    assert destination.read_text(encoding="utf-8") == "replacement"
    assert replacement_digest == sha256_file(destination)
    assert replacement_digest != digest


def test_single_artifact_rejects_paths_outside_staging_and_library(
    library_root: Path, tmp_path: Path
) -> None:
    library = _paper_library(library_root)
    outside = tmp_path / "outside.md"
    outside.write_text("no", encoding="utf-8")

    with pytest.raises(ValueError):
        library.install_artifact(outside, "papers/Smith2024/no.md")

    stage = library.stage_dir()
    artifact = stage / "artifact.md"
    artifact.write_text("no", encoding="utf-8")
    with pytest.raises(ValueError):
        library.install_artifact(artifact, "../outside.md")

    assert outside.read_text(encoding="utf-8") == "no"
    assert not (library_root / "papers" / "Smith2024" / "no.md").exists()


def test_install_rejects_symlinked_destination_parent(library_root: Path, tmp_path: Path) -> None:
    library = _paper_library(library_root)
    outside = tmp_path / "outside-operational"
    outside.mkdir()
    generated = library_root / "papers" / "Smith2024" / ".pp"
    try:
        generated.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks unavailable: {error}")
    stage = library.stage_dir()
    artifact = stage / "summary.md"
    artifact.write_text("must stay inside", encoding="utf-8")

    with pytest.raises(ValueError):
        library.install_artifact(artifact, "papers/Smith2024/.pp/operation.log")

    assert list(outside.iterdir()) == []


def test_conversion_bundle_replaces_prior_content_and_reports_installed_hashes(
    library_root: Path,
) -> None:
    library = _paper_library(library_root)
    paper = library_root / "papers" / "Smith2024"
    (paper / "transcription.md").write_text("old", encoding="utf-8")
    (paper / "figures").mkdir()
    (paper / "figures" / "old.png").write_bytes(b"old figure")
    (paper / "pages").mkdir()
    (paper / "pages" / "old.png").write_bytes(b"old page")

    stage = library.stage_dir()
    (stage / "transcription.md").write_bytes(b"# Paper\n")
    figures = stage / "figures" / "charts"
    figures.mkdir(parents=True)
    (figures / "one.png").write_bytes(b"new figure")
    pages = stage / "pages"
    pages.mkdir()
    (pages / "page1.png").write_bytes(b"new page")

    hashes = library.install_conversion_bundle("Smith2024", stage)

    expected = {
        "papers/Smith2024/transcription.md": b"# Paper\n",
        "papers/Smith2024/figures/charts/one.png": b"new figure",
        "papers/Smith2024/pages/page1.png": b"new page",
    }
    assert set(hashes) == set(expected)
    for relative_path, content in expected.items():
        installed = library_root.joinpath(*PurePosixPath(relative_path).parts)
        assert installed.read_bytes() == content
        assert hashes[relative_path] == sha256_file(installed)
    assert not (paper / "figures" / "old.png").exists()
    assert not (paper / "pages" / "old.png").exists()


def test_bundle_validation_happens_before_installed_content_changes(library_root: Path) -> None:
    library = _paper_library(library_root)
    paper = library_root / "papers" / "Smith2024"
    (paper / "transcription.md").write_text("old", encoding="utf-8")
    (paper / "figures").mkdir()
    (paper / "figures" / "old.png").write_bytes(b"old")
    before = _files_under(paper)
    stage = library.stage_dir()
    (stage / "transcription.md").write_text("", encoding="utf-8")

    with pytest.raises(ValueError):
        library.install_conversion_bundle("Smith2024", stage)

    assert _files_under(paper) == before


@pytest.mark.parametrize(
    "relative_path",
    [
        Path("transcription.md"),
        Path("figures") / "linked.png",
        Path("pages") / "page1.png",
    ],
)
def test_conversion_bundle_rejects_symlinked_staged_content(
    library_root: Path, tmp_path: Path, relative_path: Path
) -> None:
    library = _paper_library(library_root)
    stage = library.stage_dir()
    linked = stage / relative_path
    linked.parent.mkdir(parents=True, exist_ok=True)
    if relative_path.name != "transcription.md":
        (stage / "transcription.md").write_text("text", encoding="utf-8")
    outside = tmp_path / f"outside-{relative_path.name}"
    outside.write_bytes(b"outside")
    try:
        linked.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"file symlinks unavailable: {error}")

    with pytest.raises(ValueError):
        library.install_conversion_bundle("Smith2024", stage)

    assert not (library_root / "papers" / "Smith2024" / "transcription.md").exists()


def test_bundle_install_failure_preserves_previous_bundle(
    library_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = _paper_library(library_root)
    paper = library_root / "papers" / "Smith2024"
    (paper / "transcription.md").write_text("old", encoding="utf-8")
    (paper / "figures").mkdir()
    (paper / "figures" / "old.png").write_bytes(b"old")
    (paper / "pages").mkdir()
    (paper / "pages" / "page1.png").write_bytes(b"old page")
    stage = library.stage_dir()
    (stage / "transcription.md").write_text("new", encoding="utf-8")
    (stage / "figures").mkdir()
    (stage / "figures" / "new.png").write_bytes(b"new")
    (stage / "pages").mkdir()
    (stage / "pages" / "page1.png").write_bytes(b"new page")
    before = _files_under(paper)
    real_replace = os.replace

    def fail_during_install(source: Path, destination: Path) -> None:
        if Path(source) == stage / "figures" and Path(destination) == paper / "figures":
            raise OSError("interrupted bundle install")
        real_replace(source, destination)

    monkeypatch.setattr("paper_pipeline.library.storage.os.replace", fail_during_install)
    with pytest.raises(OSError):
        library.install_conversion_bundle("Smith2024", stage)

    assert _files_under(paper) == before


def test_clean_stale_staging_only_removes_old_temp_directories(
    library_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = _paper_library(library_root)
    stale = library.stage_dir()
    current = library.stage_dir()
    stale_file = stale.parent / "old-file"
    stale_file.write_text("not a staging directory", encoding="utf-8")
    installed = library_root / "papers" / "Smith2024" / "keep.txt"
    installed.write_text("keep", encoding="utf-8")
    monkeypatch.setattr("paper_pipeline.library.storage._PROCESS_STARTED_AT", 1_000.0)
    os.utime(stale, (999.0, 999.0))
    os.utime(stale_file, (999.0, 999.0))
    os.utime(current, (1_001.0, 1_001.0))

    removed = library.clean_stale_staging()

    assert removed == [stale]
    assert not stale.exists()
    assert current.exists()
    assert stale_file.read_text(encoding="utf-8") == "not a staging directory"
    assert installed.read_text(encoding="utf-8") == "keep"
