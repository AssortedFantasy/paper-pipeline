"""Atomic staging and artifact installation tests."""

import os
from pathlib import Path

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


def test_stage_dir_is_fresh_and_inside_operational_tmp(library_root: Path) -> None:
    library = create_library(library_root)

    first = library.stage_dir()
    second = library.stage_dir()

    assert first != second
    assert first.parent == library_root / ".pp" / "tmp"
    assert second.parent == first.parent


def test_install_single_artifact_validates_hashes_and_replaces(library_root: Path) -> None:
    library = _paper_library(library_root)
    first_stage = library.stage_dir()
    first = first_stage / "summary.md"
    first.write_text("first", encoding="utf-8")
    validated: list[Path] = []

    digest = library.install_artifact(
        first,
        "papers/Smith2024/summary.md",
        validate=validated.append,
    )

    destination = library_root / "papers" / "Smith2024" / "summary.md"
    assert destination.read_text(encoding="utf-8") == "first"
    assert digest == sha256_file(destination)
    assert validated == [first.resolve()]

    replacement_stage = library.stage_dir()
    replacement = replacement_stage / "summary.md"
    replacement.write_text("replacement", encoding="utf-8")
    library.install_artifact(replacement, "papers/Smith2024/summary.md")
    assert destination.read_text(encoding="utf-8") == "replacement"


def test_single_artifact_rejects_paths_outside_staging_and_library(
    library_root: Path, tmp_path: Path
) -> None:
    library = _paper_library(library_root)
    outside = tmp_path / "outside.md"
    outside.write_text("no", encoding="utf-8")

    with pytest.raises(ValueError, match="staged file"):
        library.install_artifact(outside, "papers/Smith2024/no.md")

    stage = library.stage_dir()
    artifact = stage / "artifact.md"
    artifact.write_text("no", encoding="utf-8")
    with pytest.raises(ValueError, match="relative"):
        library.install_artifact(artifact, "../outside.md")


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

    with pytest.raises(ValueError, match="must not contain symlinks"):
        library.install_artifact(artifact, "papers/Smith2024/.pp/operation.log")

    assert list(outside.iterdir()) == []


def test_install_conversion_bundle_with_figures_pages_and_hashes(library_root: Path) -> None:
    library = _paper_library(library_root)
    stage = library.stage_dir()
    (stage / "transcription.md").write_text("# Paper\n", encoding="utf-8")
    figures = stage / "figures"
    figures.mkdir()
    (figures / "one.png").write_bytes(b"image")
    pages = stage / "pages"
    pages.mkdir()
    (pages / "page1.png").write_bytes(b"page")

    hashes = library.install_conversion_bundle("Smith2024", stage)

    paper = library_root / "papers" / "Smith2024"
    assert (paper / "transcription.md").read_text(encoding="utf-8") == "# Paper\n"
    assert (paper / "figures" / "one.png").read_bytes() == b"image"
    assert (paper / "pages" / "page1.png").read_bytes() == b"page"
    assert hashes["papers/Smith2024/transcription.md"] == sha256_file(paper / "transcription.md")
    assert hashes["papers/Smith2024/figures/one.png"] == sha256_file(paper / "figures" / "one.png")
    assert hashes["papers/Smith2024/pages/page1.png"] == sha256_file(paper / "pages" / "page1.png")


def test_bundle_replacement_removes_old_figures_and_pages(library_root: Path) -> None:
    library = _paper_library(library_root)
    paper = library_root / "papers" / "Smith2024"
    (paper / "transcription.md").write_text("old", encoding="utf-8")
    (paper / "figures").mkdir()
    (paper / "figures" / "old.png").write_bytes(b"old")
    (paper / "pages").mkdir()
    (paper / "pages" / "page1.png").write_bytes(b"old")
    stage = library.stage_dir()
    (stage / "transcription.md").write_text("new", encoding="utf-8")

    library.install_conversion_bundle("Smith2024", stage)

    assert (paper / "transcription.md").read_text(encoding="utf-8") == "new"
    assert not (paper / "figures").exists()
    assert not (paper / "pages").exists()


def test_bundle_validation_happens_before_installed_content_changes(library_root: Path) -> None:
    library = _paper_library(library_root)
    paper = library_root / "papers" / "Smith2024"
    (paper / "transcription.md").write_text("old", encoding="utf-8")
    stage = library.stage_dir()
    (stage / "transcription.md").write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="non-empty"):
        library.install_conversion_bundle("Smith2024", stage)

    assert (paper / "transcription.md").read_text(encoding="utf-8") == "old"


def test_bundle_rejects_symlinked_staged_content(library_root: Path, tmp_path: Path) -> None:
    library = _paper_library(library_root)
    stage = library.stage_dir()
    outside = tmp_path / "outside-transcription.md"
    outside.write_text("outside", encoding="utf-8")
    try:
        (stage / "transcription.md").symlink_to(outside)
    except OSError as error:
        pytest.skip(f"file symlinks unavailable: {error}")

    with pytest.raises(ValueError, match="non-empty transcription"):
        library.install_conversion_bundle("Smith2024", stage)

    assert not (library_root / "papers" / "Smith2024" / "transcription.md").exists()


def test_bundle_rejects_symlinked_figure(library_root: Path, tmp_path: Path) -> None:
    library = _paper_library(library_root)
    stage = library.stage_dir()
    (stage / "transcription.md").write_text("text", encoding="utf-8")
    figures = stage / "figures"
    figures.mkdir()
    outside = tmp_path / "outside-figure.png"
    outside.write_bytes(b"outside")
    try:
        (figures / "linked.png").symlink_to(outside)
    except OSError as error:
        pytest.skip(f"file symlinks unavailable: {error}")

    with pytest.raises(ValueError, match="figures must not contain symlinks"):
        library.install_conversion_bundle("Smith2024", stage)


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
    before = {
        path.relative_to(paper): path.read_bytes() for path in paper.rglob("*") if path.is_file()
    }
    real_replace = os.replace

    def fail_during_install(source: Path, destination: Path) -> None:
        if Path(source) == stage / "figures" and Path(destination) == paper / "figures":
            raise OSError("interrupted bundle install")
        real_replace(source, destination)

    monkeypatch.setattr("paper_pipeline.library.storage.os.replace", fail_during_install)
    with pytest.raises(OSError, match="interrupted bundle"):
        library.install_conversion_bundle("Smith2024", stage)

    after = {
        path.relative_to(paper): path.read_bytes() for path in paper.rglob("*") if path.is_file()
    }
    assert after == before


def test_clean_stale_staging_only_removes_old_temp_directories(
    library_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = _paper_library(library_root)
    stale = library.stage_dir()
    current = library.stage_dir()
    installed = library_root / "papers" / "Smith2024" / "keep.txt"
    installed.write_text("keep", encoding="utf-8")
    monkeypatch.setattr("paper_pipeline.library.storage._PROCESS_STARTED_AT", 1_000.0)
    os.utime(stale, (999.0, 999.0))
    os.utime(current, (1_001.0, 1_001.0))

    removed = library.clean_stale_staging()

    assert removed == [stale]
    assert not stale.exists()
    assert current.exists()
    assert installed.read_text(encoding="utf-8") == "keep"
