"""CSV / TSV → Document adapter using Python's standard library csv module."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import List

from .models import Block, Document

CSV_SUFFIXES = {".csv", ".tsv"}


def _read_text_fallback(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _detect_delimiter(text: str, suffix: str) -> str:
    if suffix == ".tsv":
        return "\t"
    sample = "\n".join(text.splitlines()[:20])
    if sample.strip():
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            return dialect.delimiter
        except csv.Error:
            pass
    return ","


def convert_csv(path: Path) -> Document:
    """Convert a .csv or .tsv file into a Document with a Markdown table."""
    text = _read_text_fallback(path)
    delimiter = _detect_delimiter(text, path.suffix.lower())
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows: List[List[str]] = []
    for row in reader:
        cleaned = [cell.strip() for cell in row]
        if any(cleaned):
            rows.append(cleaned)

    col_count = max((len(r) for r in rows), default=0)
    document = Document(
        str(path),
        path.suffix.lower().lstrip("."),
        path.stem,
        metadata={"rows": len(rows), "columns": col_count, "delimiter": delimiter},
    )
    if rows:
        document.add(
            Block(
                "table",
                rows=rows,
                source={"rows": len(rows)},
                confidence=1.0,
            )
        )
    else:
        document.warn("CSV/TSV file is empty.")
    return document
