from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest

from paper_pipeline.ingest.rdf import parse_rdf

FIXTURES = Path(__file__).parents[1] / "fixtures" / "zotero"


def test_clean_export_normalizes_supported_item_types() -> None:
    records = parse_rdf(FIXTURES / "clean")
    by_citekey = {record.metadata.citekey: record for record in records}

    assert set(by_citekey) == {
        "SmithJournal2024",
        "LeeConference2023",
        "ChenPreprint2022",
        "DoeBook2020",
        "DoeChapter2021",
    }
    journal = by_citekey["SmithJournal2024"]
    assert journal.metadata.title == "Journal Article"
    assert journal.metadata.authors == ["Ada Smith", "Ben Jones"]
    assert journal.metadata.year == 2024
    assert journal.metadata.venue == "Journal of Tests"
    assert journal.metadata.doi == "10.1234/example.1"
    assert journal.metadata.url == "https://example.test/article"
    assert journal.metadata.abstract == "An abstract."
    assert journal.metadata.keywords == ["learning"]
    assert journal.problems == []

    assert by_citekey["LeeConference2023"].metadata.venue == "Testing Conference"
    assert by_citekey["ChenPreprint2022"].metadata.venue == "arXiv"
    assert by_citekey["DoeBook2020"].metadata.venue == "Test Press"
    assert by_citekey["DoeChapter2021"].metadata.venue == "Collected Tests"


def test_attachment_is_absolute_existing_and_hashed_while_reading() -> None:
    record = next(
        record
        for record in parse_rdf(FIXTURES / "clean" / "library.rdf")
        if record.metadata.citekey == "SmithJournal2024"
    )

    assert record.attachment_path is not None
    assert record.attachment_path.is_absolute()
    assert record.attachment_path.is_file()
    assert (
        record.attachment_sha256 == hashlib.sha256(record.attachment_path.read_bytes()).hexdigest()
    )


def test_metadata_never_contains_absolute_fixture_paths() -> None:
    fixture_path = str(FIXTURES.resolve())

    for record in parse_rdf(FIXTURES / "clean"):
        serialized = record.metadata.model_dump_json()
        assert fixture_path not in serialized
        assert "file:///" not in serialized


def test_missing_attachment_and_no_citekey_are_per_record_problems() -> None:
    records = parse_rdf(FIXTURES / "problems")
    missing = next(
        record for record in records if record.metadata.title == "No Key and Missing File"
    )

    assert missing.metadata.citekey == ""
    assert missing.attachment_path is None
    assert missing.attachment_sha256 is None
    assert "no citekey" in missing.problems
    assert any("not found" in problem for problem in missing.problems)


def test_unknown_item_type_produces_record_instead_of_crashing() -> None:
    odd = next(
        record
        for record in parse_rdf(FIXTURES / "problems")
        if record.metadata.citekey == "OddSoftware2025"
    )

    assert odd.metadata.title == "Odd Software"
    assert "unsupported item type: computerProgram" in odd.problems
    assert "missing PDF attachment" in odd.problems


def test_duplicate_doi_pair_is_preserved_for_import_planning() -> None:
    records = parse_rdf(FIXTURES / "duplicates")

    assert len(records) == 2
    assert {record.metadata.doi for record in records} == {"10.5555/shared"}
    assert {record.metadata.citekey for record in records} == {"First2020", "Second2021"}


def test_malformed_rdf_fails_with_clear_message(tmp_path: Path) -> None:
    rdf_path = tmp_path / "broken.rdf"
    rdf_path.write_text("<rdf:RDF><broken>", encoding="utf-8")

    with pytest.raises(ValueError, match=r"Could not parse Zotero RDF export.*broken\.rdf"):
        parse_rdf(rdf_path)


def test_directory_requires_exactly_one_rdf_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        parse_rdf(tmp_path)

    (tmp_path / "one.rdf").write_text("", encoding="utf-8")
    (tmp_path / "two.rdf").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match=r"found 2"):
        parse_rdf(tmp_path)


def test_attachment_cannot_escape_export_directory(tmp_path: Path) -> None:
    rdf_path = tmp_path / "escape.rdf"
    rdf_path.write_text(
        """<?xml version="1.0"?>
        <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:z="http://www.zotero.org/namespaces/export#"
         xmlns:dc="http://purl.org/dc/elements/1.1/"
         xmlns:link="http://purl.org/rss/1.0/modules/link/">
          <rdf:Description rdf:about="urn:item"><z:itemType>journalArticle</z:itemType>
           <z:citationKey>Escape2024</z:citationKey><dc:title>Escape</dc:title>
           <link:link rdf:resource="#pdf"/></rdf:Description>
          <z:Attachment rdf:about="#pdf"><z:path rdf:resource="../outside.pdf"/>
           <link:type>application/pdf</link:type></z:Attachment>
        </rdf:RDF>""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="escapes the Zotero export directory"):
        parse_rdf(rdf_path)


def test_zotero_file_uri_spaces_do_not_emit_rdflib_noise(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    pdf = tmp_path / "paper with spaces.pdf"
    pdf.write_bytes(b"pdf")
    file_uri = pdf.as_uri().replace("%20", " ")
    rdf_path = tmp_path / "library.rdf"
    rdf_path.write_text(
        f"""<?xml version="1.0"?>
        <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:z="http://www.zotero.org/namespaces/export#"
         xmlns:dc="http://purl.org/dc/elements/1.1/"
         xmlns:link="http://purl.org/rss/1.0/modules/link/">
          <rdf:Description rdf:about="urn:item"><z:itemType>journalArticle</z:itemType>
           <z:citationKey>Spaces2024</z:citationKey><dc:title>Spaces</dc:title>
           <link:link rdf:resource="#pdf"/></rdf:Description>
          <z:Attachment rdf:about="#pdf"><z:path rdf:resource="{file_uri}"/>
           <link:type>application/pdf</link:type></z:Attachment>
        </rdf:RDF>""",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="rdflib.term"):
        records = parse_rdf(rdf_path)

    assert len(records) == 1
    assert records[0].attachment_path == pdf.resolve()
    assert not any("does not look like a valid URI" in message for message in caplog.messages)
