"""HTML → Document adapter using Python's standard library html.parser."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import Block, Document

HTML_SUFFIXES = {".html", ".htm"}

_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe"}
_HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


def _read_text_fallback(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _clean_ws(text: str) -> str:
    return re.sub(r"[ \t\f\v]+", " ", re.sub(r"\s*\n\s*", "\n", text)).strip()


def _expand_html_table(raw_rows: List[List[Tuple[str, int, int]]]) -> List[List[str]]:
    """Expand colspan and rowspan cells into a rectangular Markdown-ready table."""
    grid: List[List[Optional[str]]] = []
    for r_idx, row in enumerate(raw_rows):
        while len(grid) <= r_idx:
            grid.append([])
        c_idx = 0
        for text, colspan, rowspan in row:
            while c_idx < len(grid[r_idx]) and grid[r_idx][c_idx] is not None:
                c_idx += 1
            for dr in range(max(1, rowspan)):
                rr = r_idx + dr
                while len(grid) <= rr:
                    grid.append([])
                while len(grid[rr]) < c_idx + max(1, colspan):
                    grid[rr].append(None)
                for dc in range(max(1, colspan)):
                    grid[rr][c_idx + dc] = text if (dr == 0 and dc == 0) else ""
            c_idx += max(1, colspan)
    width = max((len(r) for r in grid), default=0)
    if width == 0:
        return []
    result: List[List[str]] = []
    for r in grid:
        normalized = [cell or "" for cell in r] + [""] * (width - len(r))
        if any(cell.strip() for cell in normalized):
            result.append(normalized)
    return result


class _HTMLBlockParser(HTMLParser):
    def __init__(self, source_base: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(convert_charrefs=True)
        self.source_base = dict(source_base or {})
        self.title: Optional[str] = None
        self.blocks: List[Block] = []

        self._skip_depth = 0
        self._in_title = False
        self._title_parts: List[str] = []

        self._heading_level: Optional[int] = None
        self._heading_parts: List[str] = []

        self._in_pre = False
        self._pre_lang = ""
        self._pre_parts: List[str] = []

        self._blockquote_depth = 0
        self._blockquote_parts: List[str] = []

        self._list_stack: List[Tuple[str, int]] = []  # (kind: 'ul'|'ol', counter)
        self._list_lines: List[str] = []
        self._li_parts: List[str] = []
        self._in_li = False

        self._in_table = False
        self._table_rows: List[List[Tuple[str, int, int]]] = []
        self._current_row: List[Tuple[str, int, int]] = []
        self._in_cell = False
        self._cell_parts: List[str] = []
        self._cell_colspan = 1
        self._cell_rowspan = 1

        self._para_parts: List[str] = []
        self._link_href: Optional[str] = None
        self._link_text_parts: List[str] = []

    def _emit_paragraph(self) -> None:
        text = _clean_ws("".join(self._para_parts))
        self._para_parts.clear()
        if text:
            self.blocks.append(Block("paragraph", text, source=dict(self.source_base)))

    def _flush_li(self) -> None:
        if not self._in_li:
            return
        text = _clean_ws("".join(self._li_parts)).replace("\n", " ")
        self._li_parts.clear()
        self._in_li = False
        if text and self._list_stack:
            kind, counter = self._list_stack[-1]
            depth = max(0, len(self._list_stack) - 1)
            indent = "  " * depth
            marker = f"{counter}. " if kind == "ol" else "- "
            self._list_lines.append(f"{indent}{marker}{text}")

    def _emit_list_if_ready(self) -> None:
        if not self._list_stack and self._list_lines:
            self.blocks.append(
                Block("list", "\n".join(self._list_lines), source=dict(self.source_base))
            )
            self._list_lines.clear()

    def _append_text(self, text: str) -> None:
        if self._link_href is not None:
            self._link_text_parts.append(text)
            return
        if self._in_pre:
            self._pre_parts.append(text)
        elif self._in_cell:
            self._cell_parts.append(text)
        elif self._heading_level is not None:
            self._heading_parts.append(text)
        elif self._in_li:
            self._li_parts.append(text)
        elif self._blockquote_depth > 0:
            self._blockquote_parts.append(text)
        else:
            self._para_parts.append(text)

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}

        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return

        if tag == "title":
            self._in_title = True
            self._title_parts.clear()
            return

        if tag in _HEADING_TAGS:
            self._emit_paragraph()
            self._flush_li()
            self._emit_list_if_ready()
            self._heading_level = _HEADING_TAGS[tag]
            self._heading_parts.clear()
            return

        if tag == "pre":
            self._emit_paragraph()
            self._flush_li()
            self._emit_list_if_ready()
            self._in_pre = True
            self._pre_lang = ""
            self._pre_parts.clear()
            return

        if tag == "code" and self._in_pre:
            cls = attr_map.get("class", "")
            m = re.search(r"(?:language|lang)-([A-Za-z0-9_+#.-]+)", cls)
            if m:
                self._pre_lang = m.group(1)
            return

        if tag == "blockquote":
            self._emit_paragraph()
            self._flush_li()
            self._emit_list_if_ready()
            self._blockquote_depth += 1
            return

        if tag in {"ul", "ol"}:
            self._emit_paragraph()
            self._flush_li()
            self._list_stack.append((tag, 0))
            return

        if tag == "li":
            self._flush_li()
            if self._list_stack:
                kind, counter = self._list_stack[-1]
                self._list_stack[-1] = (kind, counter + 1)
            else:
                self._list_stack.append(("ul", 1))
            self._in_li = True
            self._li_parts.clear()
            return

        if tag == "table":
            self._emit_paragraph()
            self._flush_li()
            self._emit_list_if_ready()
            self._in_table = True
            self._table_rows = []
            return

        if tag == "tr" and self._in_table:
            self._current_row = []
            return

        if tag in {"td", "th"} and self._in_table:
            self._in_cell = True
            self._cell_parts = []
            try:
                self._cell_colspan = max(1, int(attr_map.get("colspan") or 1))
            except ValueError:
                self._cell_colspan = 1
            try:
                self._cell_rowspan = max(1, int(attr_map.get("rowspan") or 1))
            except ValueError:
                self._cell_rowspan = 1
            return

        if tag == "hr":
            self._emit_paragraph()
            self._flush_li()
            self._emit_list_if_ready()
            self.blocks.append(Block("page_break", source=dict(self.source_base)))
            return

        if tag == "br":
            self._append_text("\n")
            return

        if tag == "img":
            alt = attr_map.get("alt", "").strip()
            src = attr_map.get("src", "").strip()
            if alt and not self._in_cell and not self._in_li:
                self._emit_paragraph()
                self.blocks.append(
                    Block("image", src or alt, source=dict(self.source_base), metadata={"alt": alt})
                )
            elif alt:
                self._append_text(alt)
            return

        if tag == "a":
            href = attr_map.get("href", "").strip()
            if href and (href.startswith(("http://", "https://", "mailto:"))):
                self._link_href = href
                self._link_text_parts = []
            return

        if tag in {"p", "div", "section", "article", "header", "main"}:
            if not self._in_cell and not self._in_li and self._blockquote_depth == 0:
                self._emit_paragraph()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth > 0:
            return

        if tag == "title" and self._in_title:
            self._in_title = False
            candidate = _clean_ws("".join(self._title_parts))
            if candidate:
                self.title = candidate
            return

        if tag == "a" and self._link_href is not None:
            href = self._link_href
            link_text = _clean_ws("".join(self._link_text_parts))
            self._link_href = None
            self._link_text_parts = []
            if link_text and link_text != href and not href.startswith("mailto:"):
                self._append_text(f"{link_text} ({href})")
            else:
                self._append_text(link_text or href)
            return

        if tag in _HEADING_TAGS and self._heading_level is not None:
            text = _clean_ws("".join(self._heading_parts)).replace("\n", " ")
            level = self._heading_level
            self._heading_level = None
            self._heading_parts.clear()
            if text:
                self.blocks.append(
                    Block(
                        "heading",
                        text,
                        source=dict(self.source_base),
                        metadata={"level": level},
                    )
                )
            return

        if tag == "pre" and self._in_pre:
            code = "".join(self._pre_parts).strip("\n")
            lang = self._pre_lang
            self._in_pre = False
            self._pre_lang = ""
            self._pre_parts.clear()
            if code.strip():
                self.blocks.append(
                    Block(
                        "code",
                        code,
                        source=dict(self.source_base),
                        metadata={"language": lang},
                    )
                )
            return

        if tag == "blockquote" and self._blockquote_depth > 0:
            self._blockquote_depth -= 1
            if self._blockquote_depth == 0:
                text = _clean_ws("".join(self._blockquote_parts))
                self._blockquote_parts.clear()
                if text:
                    self.blocks.append(Block("blockquote", text, source=dict(self.source_base)))
            return

        if tag == "li":
            self._flush_li()
            return

        if tag in {"ul", "ol"}:
            self._flush_li()
            if self._list_stack:
                self._list_stack.pop()
            self._emit_list_if_ready()
            return

        if tag in {"td", "th"} and self._in_cell:
            text = _clean_ws("".join(self._cell_parts))
            self._current_row.append((text, self._cell_colspan, self._cell_rowspan))
            self._in_cell = False
            self._cell_parts = []
            return

        if tag == "tr" and self._in_table:
            if self._current_row:
                self._table_rows.append(self._current_row)
            self._current_row = []
            return

        if tag == "table" and self._in_table:
            self._in_table = False
            rows = _expand_html_table(self._table_rows)
            self._table_rows = []
            if rows:
                self.blocks.append(Block("table", rows=rows, source=dict(self.source_base)))
            return

        if tag in {"p", "div", "section", "article"}:
            if not self._in_cell and not self._in_li and self._blockquote_depth == 0:
                self._emit_paragraph()

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_title:
            self._title_parts.append(data)
            return
        self._append_text(data)

    def finalize(self) -> None:
        self._emit_paragraph()
        self._flush_li()
        self._list_stack.clear()
        self._emit_list_if_ready()
        if self._blockquote_parts:
            text = _clean_ws("".join(self._blockquote_parts))
            if text:
                self.blocks.append(Block("blockquote", text, source=dict(self.source_base)))


def html_to_blocks(
    html_text: str, source_base: Optional[Dict[str, Any]] = None
) -> Tuple[Optional[str], List[Block]]:
    """Convert an HTML string into (optional_title, blocks)."""
    parser = _HTMLBlockParser(source_base=source_base)
    parser.feed(html_text or "")
    parser.close()
    parser.finalize()
    return parser.title, parser.blocks


def convert_html(path: Path) -> Document:
    """Convert an .html/.htm file to a structured Document."""
    html_text = _read_text_fallback(path)
    title, blocks = html_to_blocks(html_text)
    document = Document(
        str(path),
        "html",
        title or path.stem,
        metadata={"blocks": len(blocks)},
    )
    for block in blocks:
        document.add(block)
    if not document.blocks:
        document.warn("HTML file contained no visible content.")
    return document
