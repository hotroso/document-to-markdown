"""Deterministic Markdown renderer for the shared document model."""

from __future__ import annotations

import re
from typing import List

from .models import Block, Document


def escape_inline(value: object) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\\", "\\\\")
    text = text.replace("`", "\\`")
    text = text.replace("[", "\\[").replace("]", "\\]")
    text = text.replace("*", "\\*").replace("_", "\\_").replace("~", "\\~")
    text = text.replace("#", "\\#").replace(">", "\\>").replace("!", "\\!")
    text = re.sub(r"^([+-])(?=\s)", r"\\\1", text)
    text = re.sub(r"^(\d+)\.(?=\s)", r"\1\\.", text)
    return text.strip()


def escape_cell(value: object) -> str:
    text = escape_inline(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\n", "<br>")
    return text.replace("|", "\\|")


def _is_number(value: str) -> bool:
    cleaned = value.replace(",", "").replace("%", "").replace("$", "").strip()
    cleaned = cleaned.strip("()")
    try:
        float(cleaned)
        return True
    except ValueError:
        return False


def render_table(rows: List[List[str]]) -> List[str]:
    if not rows:
        return []
    width = max(len(row) for row in rows)
    normalized = [list(row) + [""] * (width - len(row)) for row in rows]
    header, body = normalized[0], normalized[1:]
    alignments = []
    for col in range(width):
        values = [row[col] for row in body if row[col].strip()]
        numeric = bool(values) and sum(_is_number(v) for v in values) / len(values) >= 0.7
        alignments.append("right" if numeric else "left")
    lines = ["| " + " | ".join(escape_cell(value) for value in header) + " |"]
    separators = ["---:" if align == "right" else ":---" for align in alignments]
    lines.append("| " + " | ".join(separators) + " |")
    for row in body:
        lines.append("| " + " | ".join(escape_cell(value) for value in row) + " |")
    return lines


def _source_comment(block: Block) -> str:
    parts = [f"{key}={value}" for key, value in block.source.items() if value not in (None, "")]
    return "<!-- source: " + ", ".join(parts) + " -->" if parts else ""


def render_document(document: Document, mode: str = "balanced") -> str:
    """Render a document; ``ai`` adds compact source comments."""
    lines: List[str] = [f"# {escape_inline(document.title)}", ""]
    traceable = mode in {"fidelity", "ai"}
    if traceable:
        lines.extend([f"<!-- source-file: {document.source} -->", ""])

    for block in document.blocks:
        marker = _source_comment(block) if traceable else ""
        if marker:
            lines.append(marker)
        if block.kind == "heading":
            level = int(block.metadata.get("level", 2))
            lines.extend(["#" * max(1, min(level, 6)) + " " + escape_inline(block.text), ""])
        elif block.kind == "paragraph":
            text = re.sub(r"[ \t]+", " ", block.text.replace("\r", "")).strip()
            if text:
                lines.extend([escape_inline(text), ""])
        elif block.kind == "list":
            for item in block.text.splitlines():
                item = item.strip()
                if item:
                    item = re.sub(r"^[•●▪◦·]\s*", "- ", item)
                    item = re.sub(r"^(\d+)[.)]\s+", r"\1. ", item)
                    if not item.startswith(("- ", "* ")) and not re.match(r"^\d+\. ", item):
                        item = "- " + item
                    marker_match = re.match(r"^([-*] |\d+\. )", item)
                    marker = marker_match.group(1) if marker_match else ""
                    content = item[len(marker):] if marker else item
                    lines.append(marker + escape_inline(content))
            lines.append("")
        elif block.kind == "table":
            lines.extend(render_table(block.rows))
            lines.append("")
        elif block.kind == "image":
            alt = escape_inline(block.metadata.get("alt", "image"))
            lines.extend([f"![{alt}]({block.text})", ""])
        elif block.kind == "page_break":
            lines.extend(["---", ""])
        elif block.kind == "warning" and mode != "ai":
            lines.extend(["> **Warning:** " + escape_inline(block.text), ""])

    if document.warnings:
        lines.extend(["## Conversion warnings", ""])
        for warning in document.warnings:
            lines.append("- " + escape_inline(warning))
        lines.append("")
    result = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", result) + "\n"
