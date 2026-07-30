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

    categories = {
        "additions": {item.metadata.citekey for item in plan.additions},
        "refreshes": {item.metadata.citekey for item in plan.refreshes},
        "replacements": {item.metadata.citekey for item in plan.source_replacements},
    }
    assert categories == {
        "additions": {"Add2024"},
        "refreshes": {"Refresh2024"},
        "replacements": {"Replace2024"},
    }
    assert not (categories["additions"] & categories["refreshes"])
    assert not (categories["additions"] & categories["replacements"])
    assert not (categories["refreshes"] & categories["replacements"])

    refresh = plan.refreshes[0]
    replacement = plan.source_replacements[0]
    assert refresh.metadata.title == "Corrected title"
    assert refresh.expected_source_sha256 == library.read_paper("Refresh2024").source_sha256
    assert replacement.expected_source_sha256 == library.read_paper("Replace2024").source_sha256
    assert len(plan.problems) == 1
    assert plan.problems[0].startswith("Broken2024:")


def test_invalid_and_duplicate_citekeys_are_problems_not_actions(
    library: Library, tmp_path: Path
) -> None:
    records = [
        parsed_record(tmp_path, "bad/key"),
        parsed_record(tmp_path, "Repeated2024", title="First"),
        parsed_record(tmp_path, "Repeated2024", title="Second"),
        parsed_record(tmp_path, "Valid2024"),
    ]

    plan = build_import_plan(library, records)

    assert {item.metadata.citekey for item in plan.additions} == {"Valid2024"}
    assert plan.refreshes == []
    assert plan.source_replacements == []
    assert sum(problem.startswith("bad/key:") for problem in plan.problems) == 1
    assert sum(problem.startswith("Repeated2024:") for problem in plan.problems) == 2


def test_duplicate_candidates_are_advisory_and_never_auto_merged(
    library: Library, tmp_path: Path
) -> None:
    library.write_paper(installed_record("Existing2020", title="A Study: Testing Pipelines!"))
    records = [
        parsed_record(tmp_path, "First2024", doi="10.1234/SAME"),
        parsed_record(tmp_path, "Second2024", doi="https://doi.org/10.1234/same"),
        parsed_record(tmp_path, "Incoming2024", title="A study testing pipelines"),
    ]

    plan = build_import_plan(library, records)

    assert {item.metadata.citekey for item in plan.additions} == {
        "First2024",
        "Second2024",
        "Incoming2024",
    }
    pairs = {
        frozenset((candidate.citekey, candidate.candidate_citekey))
        for candidate in plan.duplicate_candidates
    }
    assert pairs == {
        frozenset(("First2024", "Second2024")),
        frozenset(("Existing2020", "Incoming2024")),
    }
    assert all(candidate.reason for candidate in plan.duplicate_candidates)


def test_plan_is_directly_json_serializable_and_does_not_modify_inputs(
    library: Library, tmp_path: Path
) -> None:
    record = parsed_record(tmp_path, "Serializable2024")
    original_record = (
        record.metadata.model_copy(deep=True),
        record.attachment_path,
        record.attachment_sha256,
        list(record.problems),
    )
    original_library = library.list_papers()

    plan = build_import_plan(library, [record])
    payload = json.loads(plan.model_dump_json())

    assert isinstance(payload, dict)
    assert payload["additions"][0]["metadata"]["citekey"] == "Serializable2024"
    assert isinstance(payload["additions"][0]["attachment_path"], str)
    assert (
        record.metadata,
        record.attachment_path,
        record.attachment_sha256,
        record.problems,
    ) == original_record
    assert library.list_papers() == original_library
