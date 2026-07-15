"""Read-only, structured library validation tests."""

import json
from pathlib import Path

import pytest

from paper_pipeline.library.model import (
    ConversionRecord,
    PaperMetadata,
    PaperRecord,
    RecipeRecord,
)
from paper_pipeline.library.paths import FORMAT_VERSION
from paper_pipeline.library.storage import create_library, sha256_file
from paper_pipeline.library.validation import validate_library

SOURCE_PATH = "papers/Smith2024/source/paper.pdf"


def _record() -> PaperRecord:
    return PaperRecord(
        format_version=FORMAT_VERSION,
        metadata=PaperMetadata(citekey="Smith2024", title="A paper"),
        source_pdf=SOURCE_PATH,
    )


def _library_with_source(root: Path):
    library = create_library(root)
    record = _record()
    source = root / SOURCE_PATH
    source.parent.mkdir(parents=True)
    source.write_bytes(b"pdf")
    record.source_sha256 = sha256_file(source)
    library.write_paper(record)
    return library, record


def test_healthy_library_has_no_problems(library_root: Path) -> None:
    library, _record = _library_with_source(library_root)

    report = validate_library(library)

    assert report.ok
    assert report.problems == []


@pytest.mark.parametrize("mutation", ["missing", "malformed", "newer"])
def test_library_metadata_problems_are_errors(library_root: Path, mutation: str) -> None:
    create_library(library_root)
    info = library_root / "library.json"
    if mutation == "missing":
        info.unlink()
    elif mutation == "malformed":
        info.write_text("not-json", encoding="utf-8")
    else:
        payload = json.loads(info.read_text(encoding="utf-8"))
        payload["format_version"] = 999
        info.write_text(json.dumps(payload), encoding="utf-8")

    report = validate_library(library_root)

    assert not report.ok
    assert report.problems[0].severity == "error"
    assert report.problems[0].action


def test_invalid_paper_directory_is_reported_and_validation_continues(library_root: Path) -> None:
    library, _record = _library_with_source(library_root)
    bad = library_root / "papers" / "bad key"
    bad.mkdir()

    report = validate_library(library)

    assert any(
        problem.severity == "error" and problem.citekey == "bad key" for problem in report.problems
    )


def test_missing_source_is_not_reprocessable_warning(library_root: Path) -> None:
    library, _record = _library_with_source(library_root)
    (library_root / SOURCE_PATH).unlink()

    report = validate_library(library)

    problem = next(problem for problem in report.problems if "not reprocessable" in problem.message)
    assert problem.severity == "warning"
    assert report.ok


def test_source_hash_mismatch_is_corruption_error(library_root: Path) -> None:
    library, _record = _library_with_source(library_root)
    (library_root / SOURCE_PATH).write_bytes(b"changed")

    report = validate_library(library)

    assert any(
        problem.severity == "error" and "Source PDF hash" in problem.message
        for problem in report.problems
    )


def test_stale_transcription_is_warning_but_bad_installed_hash_is_error(
    library_root: Path,
) -> None:
    library, record = _library_with_source(library_root)
    transcription = library_root / "papers" / "Smith2024" / "transcription.md"
    transcription.write_text("text", encoding="utf-8")
    record.conversion = ConversionRecord(
        source_sha256="old-source",
        transcription_sha256=sha256_file(transcription),
    )
    library.write_paper(record)

    report = validate_library(library)

    assert any(
        problem.severity == "warning" and "transcription is stale" in problem.message
        for problem in report.problems
    )
    transcription.write_text("tampered", encoding="utf-8")
    report = validate_library(library)
    assert any(
        problem.severity == "error" and "transcription hash" in problem.message
        for problem in report.problems
    )


def test_recipe_output_hash_and_input_freshness_are_checked(library_root: Path) -> None:
    library, record = _library_with_source(library_root)
    summary = library_root / "papers" / "Smith2024" / "summary.md"
    summary.write_text("summary", encoding="utf-8")
    record.recipes["summary"] = RecipeRecord(
        input_artifact="papers/Smith2024/source/paper.pdf",
        input_sha256="old-source",
        output_artifact="papers/Smith2024/summary.md",
        output_sha256=sha256_file(summary),
    )
    library.write_paper(record)

    report = validate_library(library)

    assert any(
        problem.severity == "warning" and "Recipe output" in problem.message
        for problem in report.problems
    )
    summary.write_text("tampered", encoding="utf-8")
    report = validate_library(library)
    assert any(
        problem.severity == "error" and "recipe output" in problem.message
        for problem in report.problems
    )


def test_unrecorded_flat_markdown_is_an_error(library_root: Path) -> None:
    library, _record = _library_with_source(library_root)
    paper_root = library_root / "papers" / "Smith2024"
    (paper_root / "orphan.md").write_text("unvalidated", encoding="utf-8")

    report = validate_library(library)

    assert any(
        problem.severity == "error" and "Unexpected unrecorded entry" in problem.message
        for problem in report.problems
    )


def test_paper_directory_rejects_unexpected_entries(library_root: Path) -> None:
    library, _record = _library_with_source(library_root)
    paper_root = library_root / "papers" / "Smith2024"
    (paper_root / "nested").mkdir()
    (paper_root / "payload.json").write_text("{}", encoding="utf-8")

    report = validate_library(library)

    unexpected = [
        problem for problem in report.problems if "Unexpected unrecorded" in problem.message
    ]
    assert {problem.message for problem in unexpected} == {
        "Unexpected unrecorded entry in paper directory: 'nested'.",
        "Unexpected unrecorded entry in paper directory: 'payload.json'.",
    }


def test_validator_rejects_unrecorded_symlink(library_root: Path, tmp_path: Path) -> None:
    library, _record = _library_with_source(library_root)
    outside = tmp_path / "outside-output.md"
    outside.write_text("outside", encoding="utf-8")
    generated = library_root / "papers" / "Smith2024" / "orphan.md"
    try:
        generated.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"directory symlinks unavailable: {error}")

    report = validate_library(library)

    assert any("Unexpected unrecorded entry" in p.message for p in report.problems)


def test_deleted_paper_is_reported_as_stale_index(library_root: Path) -> None:
    library = create_library(library_root)
    (library_root / "indexes" / "titles.md").write_text(
        "Deleted2024: A removed paper\n", encoding="utf-8"
    )

    report = validate_library(library)

    problem = next(problem for problem in report.problems if "Deleted2024" in problem.message)
    assert problem.severity == "warning"
    assert problem.action == "Rebuild the indexes."


def test_validation_does_not_modify_library(library_root: Path) -> None:
    library, _record = _library_with_source(library_root)
    before = {
        path.relative_to(library_root): path.read_bytes()
        for path in library_root.rglob("*")
        if path.is_file()
    }

    validate_library(library)

    after = {
        path.relative_to(library_root): path.read_bytes()
        for path in library_root.rglob("*")
        if path.is_file()
    }
    assert after == before
