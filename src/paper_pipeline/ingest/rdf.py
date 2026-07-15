"""Zotero RDF/XML parsing.

Implemented by WP-2A.1. Parses a Zotero RDF export directory
(``.rdf`` file plus ``files/`` attachment tree) into normalized
``ImportRecord`` objects: PaperMetadata + absolute path to the exported PDF
attachment plus its SHA-256 (absolute paths are fine *here*; they never enter
the library).

Zotero quirks (item types, container titles, author ordering, attachment
links) stay inside this module.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from rdflib import RDF, Graph, Literal, Namespace, URIRef
from rdflib.term import Node

from paper_pipeline.library.model import PaperMetadata

BIB = Namespace("http://purl.org/net/biblio#")
DC = Namespace("http://purl.org/dc/elements/1.1/")
DCTERMS = Namespace("http://purl.org/dc/terms/")
FOAF = Namespace("http://xmlns.com/foaf/0.1/")
LINK = Namespace("http://purl.org/rss/1.0/modules/link/")
Z = Namespace("http://www.zotero.org/namespaces/export#")

_SUPPORTED_ITEM_TYPES = {"journalArticle", "conferencePaper", "preprint", "book", "bookSection"}
_YEAR_PATTERN = re.compile(r"(?<!\d)(?:1[5-9]\d{2}|20\d{2}|21\d{2})(?!\d)")
_DOI_PATTERN = re.compile(r"10\.\d{4,9}/\S+", re.IGNORECASE)


@dataclass(slots=True)
class ImportRecord:
    """One normalized Zotero item and its selected source attachment."""

    metadata: PaperMetadata
    attachment_path: Path | None
    attachment_sha256: str | None
    problems: list[str] = field(default_factory=list)


def parse_rdf(path: Path) -> list[ImportRecord]:
    """Parse a Zotero RDF file (or directory containing one) into import records."""
    rdf_path = _find_rdf_file(path)
    graph = Graph()
    logger = logging.getLogger("rdflib.term")
    uri_filter = _ZoteroFileUriWarningFilter()
    logger.addFilter(uri_filter)
    try:
        graph.parse(rdf_path, format="xml")
    except Exception as error:
        raise ValueError(f"Could not parse Zotero RDF export {rdf_path}: {error}") from error
    finally:
        logger.removeFilter(uri_filter)

    records: list[ImportRecord] = []
    items = {
        subject
        for subject, item_type in graph.subject_objects(Z.itemType)
        if str(item_type) != "attachment"
    }
    for subject in sorted(items, key=str):
        records.append(_parse_item(graph, subject, rdf_path.parent))
    return records


class _ZoteroFileUriWarningFilter(logging.Filter):
    """Hide rdflib noise for Zotero's usable but unescaped local file URIs."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not (
            record.name == "rdflib.term"
            and message.startswith("file:")
            and "does not look like a valid URI" in message
        )


def _find_rdf_file(path: Path) -> Path:
    path = path.resolve()
    if path.is_file():
        return path
    if not path.is_dir():
        raise ValueError(f"Zotero RDF export does not exist: {path}")
    candidates = sorted(path.glob("*.rdf"), key=lambda candidate: candidate.name.casefold())
    if len(candidates) != 1:
        raise ValueError(
            f"Expected exactly one .rdf file in Zotero export directory {path}; "
            f"found {len(candidates)}"
        )
    return candidates[0]


def _parse_item(graph: Graph, subject: Node, export_root: Path) -> ImportRecord:
    item_type = _first_text(graph, subject, Z.itemType) or "unknown"
    problems: list[str] = []
    if item_type not in _SUPPORTED_ITEM_TYPES:
        problems.append(f"unsupported item type: {item_type}")

    citekey = _first_text(graph, subject, Z.citationKey) or ""
    if not citekey:
        problems.append("no citekey")

    title = _first_text(graph, subject, DC.title) or ""
    metadata = PaperMetadata(
        citekey=citekey,
        title=title,
        authors=_authors(graph, subject),
        year=_year(_first_text(graph, subject, DC.date)),
        venue=_venue(graph, subject, item_type),
        doi=_doi(graph, subject),
        url=_url(graph, subject),
        abstract=_first_text(graph, subject, DCTERMS.abstract),
        keywords=_keywords(graph, subject),
    )
    attachment_path, attachment_problem = _pdf_attachment(graph, subject, export_root)
    if attachment_problem is not None:
        problems.append(attachment_problem)
    attachment_sha256 = _sha256(attachment_path) if attachment_path is not None else None
    return ImportRecord(metadata, attachment_path, attachment_sha256, problems)


def _first_text(graph: Graph, subject: Node, predicate: URIRef) -> str | None:
    for value in graph.objects(subject, predicate):
        text = str(value).strip()
        if text:
            return text
    return None


def _authors(graph: Graph, subject: Node) -> list[str]:
    authors_node = next(iter(graph.objects(subject, BIB.authors)), None)
    if authors_node is None:
        return []
    members: list[tuple[int, Node]] = []
    for predicate, person in graph.predicate_objects(authors_node):
        predicate_text = str(predicate)
        if predicate_text.startswith(str(RDF) + "_"):
            try:
                members.append((int(predicate_text.rsplit("_", 1)[1]), person))
            except ValueError:
                continue

    names: list[str] = []
    for _, person in sorted(members):
        given = _first_text(graph, person, FOAF.givenName)
        surname = _first_text(graph, person, FOAF.surname)
        name = " ".join(part for part in (given, surname) if part)
        if not name:
            name = _first_text(graph, person, FOAF.name) or ""
        if name:
            names.append(name)
    return names


def _year(value: str | None) -> int | None:
    if value is None:
        return None
    match = _YEAR_PATTERN.search(value)
    return int(match.group()) if match else None


def _venue(graph: Graph, subject: Node, item_type: str) -> str | None:
    for predicate in (DCTERMS.isPartOf, BIB.presentedAt):
        for node in graph.objects(subject, predicate):
            title = _nested_text(graph, node, DC.title)
            if title:
                return title
    if item_type in {"preprint", "book"}:
        for node in graph.objects(subject, DC.publisher):
            publisher = _nested_text(graph, node, FOAF.name)
            if publisher:
                return publisher
    return None


def _nested_text(graph: Graph, start: Node, predicate: URIRef) -> str | None:
    """Find nearby container text without walking arbitrary graph cycles."""
    direct = _first_text(graph, start, predicate)
    if direct:
        return direct
    for child in graph.objects(start, DCTERMS.isPartOf):
        nested = _first_text(graph, child, predicate)
        if nested:
            return nested
    return None


def _identifier_values(graph: Graph, subject: Node) -> list[str]:
    values: list[str] = []
    for identifier in graph.objects(subject, DC.identifier):
        if isinstance(identifier, Literal):
            text = str(identifier).strip()
        else:
            text = _first_text(graph, identifier, RDF.value) or ""
        if text:
            values.append(text)
    return values


def _doi(graph: Graph, subject: Node) -> str | None:
    for value in _identifier_values(graph, subject):
        match = _DOI_PATTERN.search(unquote(value))
        if match:
            return match.group().rstrip(".,;)")
    return None


def _url(graph: Graph, subject: Node) -> str | None:
    for value in _identifier_values(graph, subject):
        if value.startswith(("https://", "http://")):
            return value
    subject_text = str(subject)
    return subject_text if subject_text.startswith(("https://", "http://")) else None


def _keywords(graph: Graph, subject: Node) -> list[str]:
    keywords: list[str] = []
    for keyword_node in graph.objects(subject, DC.subject):
        if isinstance(keyword_node, Literal):
            keyword = str(keyword_node).strip()
        else:
            keyword = _first_text(graph, keyword_node, RDF.value) or ""
        if keyword and keyword not in keywords:
            keywords.append(keyword)
    return keywords


def _pdf_attachment(
    graph: Graph, subject: Node, export_root: Path
) -> tuple[Path | None, str | None]:
    candidates: list[Path] = []
    missing: list[Path] = []
    for attachment in graph.objects(subject, LINK.link):
        media_type = (_first_text(graph, attachment, LINK.type) or "").lower()
        path_value = next(iter(graph.objects(attachment, Z.path)), None)
        if path_value is None:
            continue
        candidate = _resolve_attachment_path(str(path_value), export_root)
        if media_type != "application/pdf" and candidate.suffix.lower() != ".pdf":
            continue
        if candidate.is_file():
            candidates.append(candidate)
        else:
            missing.append(candidate)

    if candidates:
        return sorted(candidates, key=lambda path: path.as_posix().casefold())[0], None
    if missing:
        relative = missing[0].relative_to(export_root).as_posix()
        return None, f"PDF attachment not found: {relative}"
    return None, "missing PDF attachment"


def _resolve_attachment_path(value: str, export_root: Path) -> Path:
    parsed = urlparse(value)
    if parsed.scheme == "file":
        candidate = Path(url2pathname(unquote(parsed.path)))
    elif parsed.scheme:
        raise ValueError(f"Unsupported attachment path URI: {value}")
    else:
        candidate = export_root / unquote(value)
    candidate = candidate.resolve()
    try:
        candidate.relative_to(export_root)
    except ValueError as error:
        raise ValueError(f"Attachment path escapes the Zotero export directory: {value}") from error
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as attachment:
        for chunk in iter(lambda: attachment.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
