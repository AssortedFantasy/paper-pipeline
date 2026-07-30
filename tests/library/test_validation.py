"""Read-only, structured library validation tests."""

import json
from pathlib import Path

import pytest

from paper_pipeline.library.model import (
    ConversionRecord,
    PageRenderRecord,
    PaperMetadata,
    PaperRecord,
    RecipeRecord,
)
from paper_pipeline.library.paths import FORMAT_VERSION
from paper_pipeline.library.storage import create_library, sha256_file
from paper_pipeline.library.validation import validate_library

SOURCE_PATH = (
    "papers/Smith2024/source/c35b21d6ca39aa7cc3b79a705d989f1a6e88b99ab43988d74048799e3db926a3.pdf"
)


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


@pytest.mark.parametrize(
    ("mutation", "action_concept"),
    [("missing", "restore"), ("malformed", "restore"), ("newer", "compatible")],
)
def test_library_metadata_problems_are_actionable_errors(
    library_root: Path, mutation: str, action_concept: str
) -> None:
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
    assert len(report.problems) == 1
    problem = report.problems[0]
    assert problem.severity == "error"
    assert problem.citekey is None
    assert action_concept in problem.action.lower()


def test_corrupt_paper_is_reported_and_validation_continues(library_root: Path) -> None:
    library, _record = _library_with_source(library_root)
    bad = library_root / "papers" / "Broken2024"
    bad.mkdir()
    (bad / "paper.json").write_text("not-json", encoding="utf-8")
    (library_root / SOURCE_PATH).unlink()

    report = validate_library(library)

    assert {(problem.citekey, problem.severity) for problem in report.problems} == {
        ("Broken2024", "error"),
        ("Smith2024", "warning"),
    }
    assert all(problem.action for problem in report.problems)


@pytest.mark.parametrize(
    ("source_state", "severity", "report_ok"),
    [("missing", "warning", True), ("mismatched", "error", False)],
)
def test_source_availability_distinguishes_reprocessability_from_corruption(
    library_root: Path, source_state: str, severity: str, report_ok: bool
) -> None:
    library, _record = _library_with_source(library_root)
    source = library_root / SOURCE_PATH
    if source_state == "missing":
        source.unlink()
    else:
        source.write_bytes(b"changed")

    report = validate_library(library)

    assert report.ok is report_ok
    assert len(report.problems) == 1
    problem = report.problems[0]
    assert (problem.citekey, problem.severity) == ("Smith2024", severity)
    assert "restore" in problem.action.lower()


@pytest.mark.parametrize("artifact_kind", ["transcription", "recipe-output"])
def test_stale_input_is_a_warning_but_installed_hash_mismatch_is_an_error(
    library_root: Path, artifact_kind: str
) -> None:
    library, record = _library_with_source(library_root)
    if artifact_kind == "transcription":
        artifact = library_root / "papers" / "Smith2024" / "transcription.md"
        artifact.write_text("text", encoding="utf-8")
        record.conversion = ConversionRecord(
            source_sha256="old-source",
            transcription_sha256=sha256_file(artifact),
        )
    else:
        artifact = library_root / "papers" / "Smith2024" / "summary.md"
        artifact.write_text("summary", encoding="utf-8")
        record.recipes["summary"] = RecipeRecord(
            input_artifact=SOURCE_PATH,
            input_sha256="old-source",
            output_artifact="papers/Smith2024/summary.md",
            output_sha256=sha256_file(artifact),
        )
    library.write_paper(record)

    report = validate_library(library)

    assert [(problem.citekey, problem.severity) for problem in report.problems] == [
        ("Smith2024", "warning")
    ]
    assert "rerun" in report.problems[0].action.lower()

    artifact.write_text("tampered", encoding="utf-8")
    report = validate_library(library)

    assert {problem.severity for problem in report.problems} == {"error", "warning"}
    assert all(problem.citekey == "Smith2024" for problem in report.problems)
    assert any(
        "restore" in problem.action.lower()
        for problem in report.problems
        if problem.severity == "error"
    )
    assert any(
        "rerun" in problem.action.lower()
        for problem in report.problems
        if problem.severity == "warning"
    )


def test_rendered_pages_have_independent_freshness_and_integrity(
    library_root: Path,
) -> None:
    library, record = _library_with_source(library_root)
    page = library_root / "papers" / "Smith2024" / "pages" / "page1.png"
    page.parent.mkdir()
    page.write_bytes(b"page")
    stored = "papers/Smith2024/pages/page1.png"
    record.pages = PageRenderRecord(
        source_sha256=record.source_sha256,
        renderer="fake",
        renderer_version="1",
        dpi=96,
        page_count=1,
        artifacts={stored: sha256_file(page)},
    )
    library.write_paper(record)

    assert validate_library(library).ok

    page.write_bytes(b"tampered")
    report = validate_library(library)
    assert not report.ok
    assert any("rendered page" in problem.message.lower() for problem in report.problems)


def test_paper_directory_rejects_unexpected_entries(library_root: Path) -> None:
    library, _record = _library_with_source(library_root)
    paper_root = library_root / "papers" / "Smith2024"
    (paper_root / "nested").mkdir()
    (paper_root / "payload.json").write_text("{}", encoding="utf-8")
    (paper_root / "orphan.md").write_text("unvalidated", encoding="utf-8")

    report = validate_library(library)

    assert len(report.problems) == 3
    assert all(
        problem.severity == "error"
        and problem.citekey == "Smith2024"
        and "remove" in problem.action.lower()
        for problem in report.problems
    )


def test_deleted_paper_is_reported_as_stale_index(library_root: Path) -> None:
    library = create_library(library_root)
    (library_root / "indexes" / "titles.md").write_text(
        "Deleted2024: A removed paper\n", encoding="utf-8"
    )

    report = validate_library(library)

    assert report.ok
    assert len(report.problems) == 1
    problem = report.problems[0]
    assert (problem.citekey, problem.severity) == ("Deleted2024", "warning")
    assert "rebuild" in problem.action.lower()


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
