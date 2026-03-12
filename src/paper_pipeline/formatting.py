from __future__ import annotations

import textwrap
from pathlib import Path

from .models import PaperRecord


def format_meta(record: PaperRecord) -> str:
    lines = [
        f"# {record.title or record.citation_key}",
        "",
        f"- citation_key: {record.citation_key}",
        f"- item_type: {record.item_type}",
        f"- date: {record.date or 'unknown'}",
        f"- venue: {record.venue or 'unknown'}",
        f"- publisher: {record.publisher or 'unknown'}",
        f"- source_url: {record.url or 'unknown'}",
        f"- local_pdf: {record.local_pdf.as_posix() if record.local_pdf else 'missing'}",
    ]

    if record.tags:
        lines.append(f"- tags: {', '.join(record.tags)}")

    lines.extend(["", "## Authors", ""])
    if record.authors:
        lines.extend(f"- {author}" for author in record.authors)
    else:
        lines.append("- unknown")

    lines.extend(["", "## Abstract", ""])
    lines.append(record.abstract or "No abstract available.")

    if record.identifiers:
        lines.extend(["", "## Identifiers", ""])
        lines.extend(f"- {identifier}" for identifier in record.identifiers)

    if record.notes:
        lines.extend(["", "## Notes", ""])
        lines.extend(f"- {note}" for note in record.notes)

    if record.local_html:
        lines.extend(["", "## Local HTML", ""])
        lines.extend(f"- {path.as_posix()}" for path in record.local_html)

    return "\n".join(lines).rstrip() + "\n"


def format_placeholder(record: PaperRecord) -> str:
    return textwrap.dedent(
        f"""\
        # Pending transcription

        Nougat output has not been generated yet.

        - citation_key: {record.citation_key}
        - source_pdf: {record.local_pdf.as_posix() if record.local_pdf else "missing"}
        - status: pending
        """
    )


def write_text(path: Path, content: str, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
