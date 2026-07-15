"""Safe presentation models for paper Markdown and recipe provenance."""

from __future__ import annotations

import re
from dataclasses import dataclass

import yaml

from paper_pipeline.services.paper_detail import GeneratedArtifact, PaperDetailData


@dataclass(frozen=True)
class MarkdownBlock:
    kind: str
    text: str = ""
    level: int = 0
    items: tuple[str, ...] = ()


@dataclass(frozen=True)
class GeneratedView:
    name: str
    title: str
    blocks: tuple[MarkdownBlock, ...]
    provenance: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class PaperDetailView:
    data: PaperDetailData
    transcription: tuple[MarkdownBlock, ...]
    generated: tuple[GeneratedView, ...]


def build_paper_view(data: PaperDetailData) -> PaperDetailView:
    generated = tuple(_generated_view(item) for item in data.generated)
    return PaperDetailView(
        data=data,
        transcription=parse_markdown(data.transcription or ""),
        generated=generated,
    )


def parse_markdown(text: str) -> tuple[MarkdownBlock, ...]:
    """Render common document structure while leaving all text autoescaped."""
    lines = text.splitlines()
    blocks: list[MarkdownBlock] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    code: list[str] | None = None

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(MarkdownBlock("paragraph", text=" ".join(paragraph)))
            paragraph.clear()

    def flush_list() -> None:
        if list_items:
            blocks.append(MarkdownBlock("list", items=tuple(list_items)))
            list_items.clear()

    for line in lines:
        if line.startswith("```"):
            flush_paragraph()
            flush_list()
            if code is None:
                code = []
            else:
                blocks.append(MarkdownBlock("code", text="\n".join(code)))
                code = None
            continue
        if code is not None:
            code.append(line)
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            flush_paragraph()
            flush_list()
            blocks.append(
                MarkdownBlock("heading", text=heading.group(2).strip(), level=len(heading.group(1)))
            )
            continue
        item = re.match(r"^\s*(?:[-*+] |\d+[.)] )(.+)$", line)
        if item:
            flush_paragraph()
            list_items.append(item.group(1).strip())
            continue
        if not line.strip():
            flush_paragraph()
            flush_list()
            continue
        flush_list()
        paragraph.append(line.strip())
    if code is not None:
        blocks.append(MarkdownBlock("code", text="\n".join(code)))
    flush_paragraph()
    flush_list()
    return tuple(blocks)


def _generated_view(artifact: GeneratedArtifact) -> GeneratedView:
    front_matter, body = _front_matter(artifact.content)
    record = artifact.record
    fallback = {
        "recipe": artifact.name,
        "recipe_version": record.recipe_version,
        "provider": record.provider,
        "model": record.model,
        "input": record.input_artifact,
        "input_sha256": record.input_sha256,
        "created": record.completed_at.isoformat() if record.completed_at else None,
    }
    ordered = (
        ("Recipe", front_matter.get("recipe") or fallback["recipe"]),
        ("Version", front_matter.get("recipe_version") or fallback["recipe_version"]),
        ("Provider", front_matter.get("provider") or fallback["provider"]),
        ("Model", front_matter.get("model") or fallback["model"]),
        ("Input", front_matter.get("input") or fallback["input"]),
        ("Input SHA-256", front_matter.get("input_sha256") or fallback["input_sha256"]),
        ("Created", front_matter.get("created") or fallback["created"]),
    )
    provenance = tuple((label, str(value)) for label, value in ordered if value is not None)
    return GeneratedView(
        name=artifact.name,
        title=artifact.name.replace("_", " ").replace("-", " ").title(),
        blocks=parse_markdown(body),
        provenance=provenance,
    )


def _front_matter(content: str) -> tuple[dict[str, object], str]:
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return {}, content
    for index, line in enumerate(lines[1:], start=1):
        if line.rstrip("\r\n") != "---":
            continue
        raw = "".join(lines[1:index])
        try:
            loaded = yaml.safe_load(raw)
        except yaml.YAMLError:
            return {}, "".join(lines[index + 1 :])
        if not isinstance(loaded, dict):
            return {}, "".join(lines[index + 1 :])
        scalar: dict[str, object] = {
            str(key): value
            for key, value in loaded.items()
            if isinstance(key, str) and isinstance(value, str | int | float)
        }
        return scalar, "".join(lines[index + 1 :])
    return {}, content
