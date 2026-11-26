"""Word (.docx) → Document adapter using Python's stdlib zipfile + XML parser."""

from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .models import Block, Document

DOCX_SUFFIXES = {".docx"}

_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "v": "urn:schemas-microsoft-com:vml",
    "dc": "http://purl.org/dc/elements/1.1/",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}

_W = f"{{{_NS['w']}}}"
_R = f"{{{_NS['r']}}}"
_A = f"{{{_NS['a']}}}"
_V = f"{{{_NS['v']}}}"

_ORDERED_NUM_FMTS = {
    "decimal",
    "decimalZero",
    "lowerLetter",
    "upperLetter",
    "lowerRoman",
    "upperRoman",
}


def _read_xml(zf: zipfile.ZipFile, member: str) -> Optional[ET.Element]:
    try:
        data = zf.read(member)
    except KeyError:
        return None
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        return None


def _read_rels(zf: zipfile.ZipFile, rels_path: str) -> Dict[str, Tuple[str, str]]:
    """Return {rId: (target, target_mode)} from an OOXML .rels file."""
    root = _read_xml(zf, rels_path)
    if root is None:
        return {}
    rels: Dict[str, Tuple[str, str]] = {}
    for rel in root:
        if rel.tag.endswith("Relationship"):
            rid = rel.attrib.get("Id")
            target = rel.attrib.get("Target", "")
            mode = rel.attrib.get("TargetMode", "")
            if rid:
                rels[rid] = (target, mode)
    return rels


def _resolve_zip_path(base_dir: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(base_dir, target))


def _read_core_title(zf: zipfile.ZipFile) -> Optional[str]:
    root = _read_xml(zf, "docProps/core.xml")
    if root is None:
        return None
    title_el = root.find("dc:title", _NS)
    if title_el is not None and title_el.text and title_el.text.strip():
        return title_el.text.strip()
    return None


def _parse_styles(zf: zipfile.ZipFile) -> Dict[str, Tuple[str, Optional[int]]]:
    """Return {styleId: (styleNameLower, outline_level_1_to_6_or_None)}."""
    root = _read_xml(zf, "word/styles.xml")
    if root is None:
        return {}
    styles: Dict[str, Tuple[str, Optional[int]]] = {}
    for style in root.findall("w:style", _NS):
        style_id = style.attrib.get(f"{_W}styleId")
        if not style_id:
            continue
        name_el = style.find("w:name", _NS)
        name = (name_el.attrib.get(f"{_W}val", "") if name_el is not None else "").strip().lower()
        outline_level: Optional[int] = None
        outline_el = style.find("w:pPr/w:outlineLvl", _NS)
        if outline_el is not None:
            raw_val = outline_el.attrib.get(f"{_W}val")
            if raw_val is not None and raw_val.isdigit():
                lvl = int(raw_val)
                if 0 <= lvl <= 5:
                    outline_level = lvl + 1
        styles[style_id] = (name, outline_level)
    return styles


def _parse_numbering(zf: zipfile.ZipFile) -> Dict[str, Dict[int, str]]:
    """Return {numId: {ilvl: numFmt}}."""
    root = _read_xml(zf, "word/numbering.xml")
    if root is None:
        return {}
    abstract_map: Dict[str, Dict[int, str]] = {}
    for ab in root.findall("w:abstractNum", _NS):
        ab_id = ab.attrib.get(f"{_W}abstractNumId")
        if ab_id is None:
            continue
        levels: Dict[int, str] = {}
        for lvl in ab.findall("w:lvl", _NS):
            ilvl_raw = lvl.attrib.get(f"{_W}ilvl", "0")
            fmt_el = lvl.find("w:numFmt", _NS)
            fmt = fmt_el.attrib.get(f"{_W}val", "bullet") if fmt_el is not None else "bullet"
            try:
                levels[int(ilvl_raw)] = fmt
            except ValueError:
                pass
        abstract_map[ab_id] = levels

    num_map: Dict[str, Dict[int, str]] = {}
    for num in root.findall("w:num", _NS):
        num_id = num.attrib.get(f"{_W}numId")
        ab_ref = num.find("w:abstractNumId", _NS)
        if num_id is not None and ab_ref is not None:
            ab_id = ab_ref.attrib.get(f"{_W}val")
            if ab_id in abstract_map:
                num_map[num_id] = abstract_map[ab_id]
    return num_map


def _heading_level(p: ET.Element, styles: Dict[str, Tuple[str, Optional[int]]]) -> Optional[int]:
    ppr = p.find("w:pPr", _NS)
    if ppr is None:
        return None
    outline_el = ppr.find("w:outlineLvl", _NS)
    if outline_el is not None:
        raw = outline_el.attrib.get(f"{_W}val")
        if raw is not None and raw.isdigit() and 0 <= int(raw) <= 5:
            return int(raw) + 1

    style_el = ppr.find("w:pStyle", _NS)
    if style_el is not None:
        sid = style_el.attrib.get(f"{_W}val", "")
        style_name, style_outline = styles.get(sid, (sid.lower(), None))
        if style_outline is not None:
            return style_outline
        for candidate in (style_name, sid.lower()):
            if candidate == "title":
                return 1
            if candidate == "subtitle":
                return 2
            m = re.match(r"^(?:heading|titre|überschrift|tiêu đề)\s*(\d)$", candidate)
            if m:
                return max(1, min(6, int(m.group(1))))
    return None


def _list_info(
    p: ET.Element, numbering: Dict[str, Dict[int, str]], styles: Dict[str, Tuple[str, Optional[int]]]
) -> Optional[Tuple[int, bool]]:
    """Return (indent_level, is_ordered) if paragraph is a list item, else None."""
    ppr = p.find("w:pPr", _NS)
    if ppr is None:
        return None
    num_pr = ppr.find("w:numPr", _NS)
    if num_pr is not None:
        ilvl_el = num_pr.find("w:ilvl", _NS)
        num_id_el = num_pr.find("w:numId", _NS)
        num_id = num_id_el.attrib.get(f"{_W}val") if num_id_el is not None else None
        if num_id == "0":
            return None
        try:
            ilvl = int(ilvl_el.attrib.get(f"{_W}val", "0")) if ilvl_el is not None else 0
        except ValueError:
            ilvl = 0
        fmt = numbering.get(num_id or "", {}).get(ilvl, "bullet")
        return (max(0, ilvl), fmt in _ORDERED_NUM_FMTS)

    style_el = ppr.find("w:pStyle", _NS)
    if style_el is not None:
        sid = style_el.attrib.get(f"{_W}val", "")
        style_name = styles.get(sid, (sid.lower(), None))[0]
        if "list bullet" in style_name or style_name == "listbullet":
            return (0, False)
        if "list number" in style_name or style_name == "listnumber":
            return (0, True)
    return None


def _extract_runs_text(
    node: ET.Element, rels: Dict[str, Tuple[str, str]]
) -> Tuple[str, List[str]]:
    """Extract paragraph text (ignoring tracked deletions) and embedded image rIds."""
    parts: List[str] = []
    image_rids: List[str] = []

    def walk(el: ET.Element) -> None:
        tag = el.tag
        if tag == f"{_W}del" or tag == f"{_W}instrText":
            return
        if tag == f"{_W}hyperlink":
            rid = el.attrib.get(f"{_R}id")
            sub_parts: List[str] = []
            for child in el:
                txt, imgs = _extract_runs_text(child, rels)
                sub_parts.append(txt)
                image_rids.extend(imgs)
            link_text = "".join(sub_parts).strip()
            target = rels.get(rid or "", ("", ""))[0] if rid else ""
            if target.startswith(("http://", "https://")) and link_text and link_text != target:
                parts.append(f"{link_text} ({target})")
            else:
                parts.append(link_text or target)
            return
        if tag == f"{_W}t":
            if el.text:
                parts.append(el.text)
            return
        if tag == f"{_W}tab":
            parts.append(" ")
            return
        if tag in {f"{_W}br", f"{_W}cr"}:
            parts.append("\n")
            return
        if tag == f"{_A}blip":
            rid = el.attrib.get(f"{_R}embed")
            if rid:
                image_rids.append(rid)
            return
        if tag == f"{_V}imagedata":
            rid = el.attrib.get(f"{_R}id")
            if rid:
                image_rids.append(rid)
            return
        for child in el:
            walk(child)

    walk(node)
    text = "".join(parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text).strip()
    return text, image_rids


def _extract_table(tbl: ET.Element, rels: Dict[str, Tuple[str, str]]) -> List[List[str]]:
    rows: List[List[str]] = []
    for tr in tbl.findall("w:tr", _NS):
        row_cells: List[str] = []
        for tc in tr.findall("w:tc", _NS):
            tcpr = tc.find("w:tcPr", _NS)
            grid_span = 1
            is_vmerge_cont = False
            if tcpr is not None:
                span_el = tcpr.find("w:gridSpan", _NS)
                if span_el is not None:
                    try:
                        grid_span = max(1, int(span_el.attrib.get(f"{_W}val", "1")))
                    except ValueError:
                        grid_span = 1
                vmerge_el = tcpr.find("w:vMerge", _NS)
                if vmerge_el is not None and vmerge_el.attrib.get(f"{_W}val") != "restart":
                    is_vmerge_cont = True

            if is_vmerge_cont:
                cell_text = ""
            else:
                para_texts: List[str] = []
                for p in tc.findall("w:p", _NS):
                    t, _ = _extract_runs_text(p, rels)
                    if t:
                        para_texts.append(t)
                cell_text = "\n".join(para_texts)

            row_cells.append(cell_text)
            for _ in range(grid_span - 1):
                row_cells.append("")
        if any(c.strip() for c in row_cells):
            rows.append(row_cells)
    return rows


def _iter_body_elements(container: ET.Element) -> List[ET.Element]:
    items: List[ET.Element] = []
    for child in container:
        if child.tag in {f"{_W}p", f"{_W}tbl"}:
            items.append(child)
        elif child.tag == f"{_W}sdt":
            content = child.find("w:sdtContent", _NS)
            if content is not None:
                items.extend(_iter_body_elements(content))
    return items


def convert_docx(path: Path, output_assets: Optional[Path] = None) -> Document:
    """Convert a .docx Word document into a structured Document."""
    if not zipfile.is_zipfile(path):
        raise RuntimeError(f"Not a valid .docx archive: {path}")

    with zipfile.ZipFile(path, "r") as zf:
        doc_root = _read_xml(zf, "word/document.xml")
        if doc_root is None:
            raise RuntimeError(f"Missing word/document.xml inside {path}")
        body = doc_root.find("w:body", _NS)
        if body is None:
            raise RuntimeError(f"Malformed word/document.xml inside {path}")

        title = _read_core_title(zf) or path.stem
        styles = _parse_styles(zf)
        numbering = _parse_numbering(zf)
        rels = _read_rels(zf, "word/_rels/document.xml.rels")

        document = Document(str(path), "docx", title, metadata={})
        list_lines: List[str] = []
        list_counters: Dict[int, int] = {}
        image_count = 0
        table_count = 0
        para_index = 0

        def flush_list() -> None:
            if list_lines:
                document.add(Block("list", "\n".join(list_lines), source={"section": "body"}))
                list_lines.clear()
                list_counters.clear()

        for elem in _iter_body_elements(body):
            if elem.tag == f"{_W}tbl":
                flush_list()
                rows = _extract_table(elem, rels)
                if rows:
                    table_count += 1
                    document.add(
                        Block(
                            "table",
                            rows=rows,
                            source={"table": table_count},
                            confidence=1.0,
                        )
                    )
                continue

            para_index += 1
            text, img_rids = _extract_runs_text(elem, rels)
            heading_lvl = _heading_level(elem, styles)
            list_meta = None if heading_lvl is not None else _list_info(elem, numbering, styles)

            if heading_lvl is not None and text:
                flush_list()
                document.add(
                    Block(
                        "heading",
                        text.replace("\n", " "),
                        source={"paragraph": para_index},
                        metadata={"level": heading_lvl},
                    )
                )
            elif list_meta is not None and text:
                ilvl, is_ordered = list_meta
                for k in list(list_counters.keys()):
                    if k > ilvl:
                        del list_counters[k]
                indent = "  " * min(ilvl, 4)
                if is_ordered:
                    list_counters[ilvl] = list_counters.get(ilvl, 0) + 1
                    marker = f"{list_counters[ilvl]}. "
                else:
                    marker = "- "
                list_lines.append(f"{indent}{marker}{text.replace(chr(10), ' ')}")
            elif text:
                flush_list()
                document.add(Block("paragraph", text, source={"paragraph": para_index}))
            elif img_rids:
                flush_list()

            if output_assets is not None and img_rids:
                output_assets.mkdir(parents=True, exist_ok=True)
                for rid in img_rids:
                    target, mode = rels.get(rid, ("", ""))
                    if not target or mode.lower() == "external":
                        continue
                    zip_member = _resolve_zip_path("word", target)
                    try:
                        img_bytes = zf.read(zip_member)
                    except KeyError:
                        continue
                    image_count += 1
                    ext = Path(zip_member).suffix.lower() or ".png"
                    filename = f"docx-image-{image_count:02d}{ext}"
                    (output_assets / filename).write_bytes(img_bytes)
                    document.add(
                        Block(
                            "image",
                            filename,
                            source={"paragraph": para_index},
                            metadata={"alt": f"Embedded image {image_count}"},
                        )
                    )

        flush_list()

        # Optional footnotes
        fn_root = _read_xml(zf, "word/footnotes.xml")
        if fn_root is not None:
            fn_lines: List[str] = []
            for fn in fn_root.findall("w:footnote", _NS):
                fn_type = fn.attrib.get(f"{_W}type", "")
                if fn_type in {"separator", "continuationSeparator"}:
                    continue
                fn_id = fn.attrib.get(f"{_W}id", "")
                texts = []
                for p in fn.findall("w:p", _NS):
                    t, _ = _extract_runs_text(p, rels)
                    if t:
                        texts.append(t)
                joined = " ".join(texts).strip()
                if joined:
                    prefix = f"{fn_id}. " if fn_id.isdigit() else "- "
                    fn_lines.append(prefix + joined)
            if fn_lines:
                document.add(
                    Block("heading", "Footnotes", source={"section": "footnotes"}, metadata={"level": 2})
                )
                document.add(Block("list", "\n".join(fn_lines), source={"section": "footnotes"}))

        document.metadata.update({"tables": table_count, "exported_images": image_count})
        if not document.blocks:
            document.warn("DOCX file contained no extractable text or tables.")
    return document
