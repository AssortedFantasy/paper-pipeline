from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from paper_pipeline.ingest.rdf import parse_rdf

FIXTURES = Path(__file__).parents[1] / "fixtures" / "zotero"


def write_single_attachment_export(rdf_path: Path, attachment_value: str) -> None:
    rdf_path.write_text(
        f"""<?xml version="1.0"?>
        <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:z="http://www.zotero.org/namespaces/export#"
         xmlns:dc="http://purl.org/dc/elements/1.1/"
         xmlns:link="http://purl.org/rss/1.0/modules/link/">
          <rdf:Description rdf:about="urn:item">
           <z:itemType>journalArticle</z:itemType>
           <z:citationKey>Attachment2024</z:citationKey>
           <dc:title>Attachment</dc:title>
           <link:link rdf:resource="#pdf"/>
          </rdf:Description>
          <z:Attachment rdf:about="#pdf">
           <z:path rdf:resource="{attachment_value}"/>
           <link:type>application/pdf</link:type>
          </z:Attachment>
        </rdf:RDF>""",
        encoding="utf-8",
    )


def test_supported_item_types_produce_normalized_metadata() -> None:
    records = parse_rdf(FIXTURES / "clean")
    by_citekey = {record.metadata.citekey: record for record in records}

    # Each supported Zotero item shape contributes one record. Venue extraction
    # deliberately differs by type (journal/container, conference, publisher).
    expected_venues = {
        "SmithJournal2024": "Journal of Tests",
        "LeeConference2023": "Testing Conference",
        "ChenPreprint2022": "arXiv",
        "DoeBook2020": "Test Press",
        "DoeChapter2021": "Collected Tests",
    }
    assert len(records) == len(expected_venues)
    assert {
        citekey: by_citekey[citekey].metadata.venue for citekey in expected_venues
    } == expected_venues

    # Representative transformations, rather than a snapshot of every field.
    journal = by_citekey["SmithJournal2024"].metadata
    assert journal.authors == ["Ada Smith", "Ben Jones"]
    assert journal.year == 2024
    assert journal.doi == "10.1234/example.1"
    assert journal.url == "https://example.test/article"
    assert all(not record.problems for record in records)


def test_attachment_is_confined_existing_and_hashed_during_parse() -> None:
    export_root = (FIXTURES / "clean").resolve()
    record = next(
        record
        for record in parse_rdf(export_root / "library.rdf")
        if record.metadata.citekey == "SmithJournal2024"
    )

    assert record.attachment_path is not None
    assert record.attachment_path.is_relative_to(export_root)
    assert record.attachment_path.is_file()
    expected_hash = hashlib.sha256(record.attachment_path.read_bytes()).hexdigest()
    assert record.attachment_sha256 == expected_hash


def test_record_problems_do_not_abort_the_rest_of_the_export() -> None:
    records = parse_rdf(FIXTURES / "problems")

    missing = next(record for record in records if not record.metadata.citekey)
    assert missing.attachment_path is None
    assert missing.attachment_sha256 is None
    assert "no citekey" in missing.problems
    assert any("not found" in problem for problem in missing.problems)

    unsupported = next(record for record in records if record.metadata.citekey == "OddSoftware2025")
    assert any("unsupported item type" in problem for problem in unsupported.problems)
    assert any("missing PDF attachment" in problem for problem in unsupported.problems)


def test_duplicate_doi_records_are_preserved_for_import_planning() -> None:
    records = parse_rdf(FIXTURES / "duplicates")

    assert len(records) == 2
    citekeys = {record.metadata.citekey for record in records}
    duplicate_dois = {record.metadata.doi for record in records}
    assert len(citekeys) == 2
    assert all(citekeys)
    assert len(duplicate_dois) == 1
    assert None not in duplicate_dois


def test_malformed_rdf_identifies_the_bad_export(tmp_path: Path) -> None:
    rdf_path = tmp_path / "broken.rdf"
    rdf_path.write_text("<rdf:RDF><broken>", encoding="utf-8")

    with pytest.raises(ValueError) as raised:
        parse_rdf(rdf_path)

    message = str(raised.value)
    assert "parse" in message.lower()
    assert rdf_path.name in message


@pytest.mark.parametrize(
    ("shape", "expected_problem"),
    [
        ("missing", "does not exist"),
        ("empty-directory", "exactly one"),
        ("multiple-rdf-files", "exactly one"),
    ],
)
def test_invalid_export_shapes_fail_before_import(
    tmp_path: Path, shape: str, expected_problem: str
) -> None:
    export_path = tmp_path / "export"
    if shape != "missing":
        export_path.mkdir()
        if shape == "multiple-rdf-files":
            (export_path / "one.rdf").touch()
            (export_path / "two.rdf").touch()

    with pytest.raises(ValueError) as raised:
        parse_rdf(export_path)

    assert expected_problem in str(raised.value)


def test_attachment_cannot_escape_export_directory(tmp_path: Path) -> None:
    rdf_path = tmp_path / "escape.rdf"
    write_single_attachment_export(rdf_path, "../outside.pdf")

    with pytest.raises(ValueError, match="escapes the Zotero export directory"):
        parse_rdf(rdf_path)


@pytest.mark.parametrize(
    "uri_style",
    ["percent-encoded", "zotero-unescaped"],
)
def test_file_uri_decoding_resolves_paths_with_spaces(tmp_path: Path, uri_style: str) -> None:
    pdf = tmp_path / "paper with spaces.pdf"
    pdf.write_bytes(b"pdf")
    file_uri = pdf.as_uri()
    if uri_style == "zotero-unescaped":
        file_uri = file_uri.replace("%20", " ")
    rdf_path = tmp_path / "library.rdf"
    write_single_attachment_export(rdf_path, file_uri)

    records = parse_rdf(rdf_path)

    assert len(records) == 1
    assert records[0].attachment_path == pdf.resolve()
