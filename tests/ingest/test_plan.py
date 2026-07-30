from __future__ import annotations

import json
from pathlib import Path

import pytest

from paper_pipeline.ingest.plan import build_import_plan
from paper_pipeline.ingest.rdf import ImportRecord
from paper_pipeline.library.model import PaperMetadata, PaperRecord
from paper_pipeline.library.paths import FORMAT_VERSION
from paper_pipeline.library.storage import Library, create_library


def parsed_record(
    tmp_path: Path,
    citekey: str,
    *,
    title: str | None = None,
    doi: str | None = None,
    source_hash: str = "source-hash",
    problems: list[str] | None = None,
) -> ImportRecord:
    return ImportRecord(
        metadata=PaperMetadata(citekey=citekey, title=title or f"Title for {citekey}", doi=doi),
        attachment_path=tmp_path / f"{citekey or 'missing'}.pdf",
        attachment_sha256=source_hash,
        problems=list(problems or []),
    )


def installed_record(
    citekey: str,
    *,
    title: str | None = None,
    doi: str | None = None,
    source_hash: str = "source-hash",
) -> PaperRecord:
    return PaperRecord(
        format_version=FORMAT_VERSION,
        metadata=PaperMetadata(citekey=citekey, title=title or f"Title for {citekey}", doi=doi),
        source_pdf=f"papers/{citekey}/source/paper.pdf",
        source_sha256=source_hash,
    )


@pytest.fixture
def library(library_root: Path) -> Library:
    return create_library(library_root)


def test_mixed_plan_keeps_categories_disjoint(library: Library, tmp_path: Path) -> None:
    library.write_paper(installed_record("Refresh2024", title="Old title"))
    library.write_paper(installed_record("Replace2024", source_hash="old"))
    records = [
        parsed_record(tmp_path, "Add2024"),
        parsed_record(tmp_path, "Refresh2024", title="Corrected title"),
        parsed_record(tmp_path, "Replace2024", source_hash="new"),
        parsed_record(tmp_path, "Broken2024", problems=["missing PDF attachment"]),
    ]

    plan = build_import_plan(library, records)

    assert [item.metadata.citekey for item in plan.additions] == ["Add2024"]
    assert [item.metadata.citekey for item in plan.refreshes] == ["Refresh2024"]
    assert [item.metadata.citekey for item in plan.source_replacements] == ["Replace2024"]
    assert plan.refreshes[0].metadata.title == "Corrected title"
    assert plan.refreshes[0].expected_source_sha256 == "source-hash"
    assert plan.source_replacements[0].expected_source_sha256 == "old"
    assert plan.problems == ["Broken2024: missing PDF attachment"]


@pytest.mark.parametrize("citekey", ["bad/key", "CON", ""])
def test_invalid_citekey_is_routed_to_problems(
    library: Library, tmp_path: Path, citekey: str
) -> None:
    plan = build_import_plan(library, [parsed_record(tmp_path, citekey)])

    assert plan.additions == []
    assert plan.refreshes == []
    assert plan.source_replacements == []
    assert len(plan.problems) == 1
    assert "citekey" in plan.problems[0]


def test_same_doi_duplicate_candidate_is_surfaced_not_merged(
    library: Library, tmp_path: Path
) -> None:
    records = [
        parsed_record(tmp_path, "First2024", doi="10.1234/SAME"),
        parsed_record(tmp_path, "Second2024", doi="https://doi.org/10.1234/same"),
    ]

    plan = build_import_plan(library, records)

    assert {item.metadata.citekey for item in plan.additions} == {"First2024", "Second2024"}
    assert len(plan.duplicate_candidates) == 1
    duplicate = plan.duplicate_candidates[0]
    assert {duplicate.citekey, duplicate.candidate_citekey} == {"First2024", "Second2024"}
    assert duplicate.reason == "same DOI: 10.1234/same"


def test_normalized_title_duplicate_against_library_is_surfaced(
    library: Library, tmp_path: Path
) -> None:
    library.write_paper(installed_record("Existing2020", title="A Study: Testing Pipelines!"))
    incoming = parsed_record(tmp_path, "Incoming2024", title="A study testing pipelines")

    plan = build_import_plan(library, [incoming])

    assert [item.metadata.citekey for item in plan.additions] == ["Incoming2024"]
    assert len(plan.duplicate_candidates) == 1
    assert plan.duplicate_candidates[0].reason == "normalized title match"


def test_duplicate_citekey_in_one_export_is_a_problem(library: Library, tmp_path: Path) -> None:
    records = [
        parsed_record(tmp_path, "Repeated2024", title="First"),
        parsed_record(tmp_path, "Repeated2024", title="Second"),
    ]

    plan = build_import_plan(library, records)

    assert plan.additions == []
    assert len(plan.problems) == 2
    assert all("duplicate citekey" in problem for problem in plan.problems)


def test_plan_is_directly_json_serializable_and_does_not_modify_inputs(
    library: Library, tmp_path: Path
) -> None:
    record = parsed_record(tmp_path, "Serializable2024")
    original_metadata = record.metadata.model_copy(deep=True)

    plan = build_import_plan(library, [record])
    payload = json.loads(plan.model_dump_json())

    assert payload["additions"][0]["metadata"]["citekey"] == "Serializable2024"
    assert isinstance(payload["additions"][0]["attachment_path"], str)
    assert record.metadata == original_metadata
    assert library.list_papers() == ([], [])
