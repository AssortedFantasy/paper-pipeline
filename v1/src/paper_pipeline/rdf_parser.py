from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from .models import PaperRecord

NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "z": "http://www.zotero.org/namespaces/export#",
    "dc": "http://purl.org/dc/elements/1.1/",
    "foaf": "http://xmlns.com/foaf/0.1/",
    "bib": "http://purl.org/net/biblio#",
    "dcterms": "http://purl.org/dc/terms/",
    "link": "http://purl.org/rss/1.0/modules/link/",
    "prism": "http://prismstandard.org/namespaces/1.2/basic/",
}


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.split())


def dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value not in seen:
            deduped.append(value)
            seen.add(value)
    return deduped


def get_rdf_resource(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return element.attrib.get(f"{{{NS['rdf']}}}resource", "")


def get_child_text(element: ET.Element, path: str) -> str:
    child = element.find(path, NS)
    if child is None:
        return ""
    return clean_text(child.text)


def get_nested_uri_text(element: ET.Element) -> str:
    for path in (
        "dc:identifier/dcterms:URI/rdf:value",
        "dc:identifier/rdf:value",
    ):
        value = get_child_text(element, path)
        if value:
            return value
    return ""


def parse_authors(element: ET.Element) -> list[str]:
    authors: list[str] = []
    for person in element.findall("bib:authors/rdf:Seq/rdf:li/foaf:Person", NS):
        given = get_child_text(person, "foaf:givenName")
        surname = get_child_text(person, "foaf:surname")
        name = " ".join(part for part in (given, surname) if part).strip()
        if name:
            authors.append(name)
    return authors


def parse_tags(element: ET.Element) -> list[str]:
    tags: list[str] = []
    for subject in element.findall("dc:subject", NS):
        for path in ("rdf:value", "z:AutomaticTag/rdf:value"):
            value = get_child_text(subject, path)
            if value:
                tags.append(value)
                break
    return tags


def parse_identifiers(element: ET.Element) -> list[str]:
    values: list[str] = []
    for identifier in element.findall("dc:identifier", NS):
        direct = clean_text(identifier.text)
        if direct:
            values.append(direct)
        uri_child = identifier.find("dcterms:URI/rdf:value", NS)
        if uri_child is not None and clean_text(uri_child.text):
            values.append(clean_text(uri_child.text))
    return dedupe_preserve_order(values)


def resolve_venue(element: ET.Element, about_lookup: dict[str, ET.Element]) -> str:
    presented = get_child_text(element, "bib:presentedAt/bib:Conference/dc:title")
    if presented:
        return presented

    inline_part_of = get_child_text(element, "dcterms:isPartOf/bib:Journal/dc:title")
    if inline_part_of:
        return inline_part_of

    part_of = element.find("dcterms:isPartOf", NS)
    part_of_ref = get_rdf_resource(part_of)
    if part_of_ref and part_of_ref in about_lookup:
        return get_child_text(about_lookup[part_of_ref], "dc:title")

    return ""


def resolve_publisher(element: ET.Element) -> str:
    for path in (
        "dc:publisher/foaf:Organization/foaf:name",
        "dc:publisher",
    ):
        value = get_child_text(element, path)
        if value:
            return value
    return ""


def load_records(
    rdf_path: Path, workspace_root: Path, allowed_types: set[str]
) -> list[PaperRecord]:
    root = ET.parse(rdf_path).getroot()  # noqa: S314 - trusted local file

    about_lookup: dict[str, ET.Element] = {}
    memo_lookup: dict[str, str] = {}
    attachment_lookup: dict[str, dict[str, str]] = {}

    for element in root:
        about = element.attrib.get(f"{{{NS['rdf']}}}about")
        if about:
            about_lookup[about] = element

        if element.tag == f"{{{NS['bib']}}}Memo":
            value = get_child_text(element, "rdf:value")
            if about and value:
                memo_lookup[about] = value

        if element.tag == f"{{{NS['z']}}}Attachment":
            attachment_lookup[about or ""] = {
                "path": get_rdf_resource(element.find("z:path", NS)),
                "mime": get_child_text(element, "link:type"),
                "title": get_child_text(element, "dc:title"),
            }

    records: list[PaperRecord] = []
    for element in root:
        item_type = get_child_text(element, "z:itemType")
        citation_key = get_child_text(element, "z:citationKey")
        if not item_type or not citation_key or item_type not in allowed_types:
            continue

        attachment_refs = [
            get_rdf_resource(link) for link in element.findall("link:link", NS)
        ]
        pdf_path: Path | None = None
        html_paths: list[Path] = []
        for ref in attachment_refs:
            attachment = attachment_lookup.get(ref)
            if not attachment:
                continue
            raw_path = attachment.get("path", "")
            mime = attachment.get("mime", "")
            resolved = workspace_root / Path(raw_path) if raw_path else None
            if mime == "application/pdf" and resolved is not None and pdf_path is None:
                pdf_path = resolved
            elif mime == "text/html" and resolved is not None:
                html_paths.append(resolved)

        notes: list[str] = []
        for memo_ref in element.findall("dcterms:isReferencedBy", NS):
            memo_key = get_rdf_resource(memo_ref)
            if memo_key and memo_key in memo_lookup:
                notes.append(memo_lookup[memo_key])
        notes = dedupe_preserve_order(notes)

        url = get_nested_uri_text(element)
        if not url:
            url = element.attrib.get(f"{{{NS['rdf']}}}about", "")

        records.append(
            PaperRecord(
                citation_key=citation_key,
                item_type=item_type,
                title=get_child_text(element, "dc:title"),
                authors=parse_authors(element),
                abstract=get_child_text(element, "dcterms:abstract"),
                date=get_child_text(element, "dc:date"),
                venue=resolve_venue(element, about_lookup),
                publisher=resolve_publisher(element),
                url=url,
                identifiers=parse_identifiers(element),
                tags=parse_tags(element),
                notes=notes,
                local_pdf=pdf_path,
                local_html=html_paths,
            )
        )

    records.sort(key=lambda record: record.citation_key.lower())
    return records
