"""PowerPoint (.pptx) → Document adapter using Python's stdlib zipfile + XML parser."""

from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .models import Block, Document

PPTX_SUFFIXES = {".pptx"}

_NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "dc": "http://purl.org/dc/elements/1.1/",
}

_P = f"{{{_NS['p']}}}"
_A = f"{{{_NS['a']}}}"
_R = f"{{{_NS['r']}}}"


def _read_xml(zf: zipfile.ZipFile, member: str) -> Optional[ET.Element]:
    try:
        data = zf.read(member)
    except KeyError:
        return None
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        return None


def _read_rels(zf: zipfile.ZipFile, rels_path: str) -> Dict[str, Tuple[str, str, str]]:
    """Return {rId: (target, rel_type, target_mode)}."""
    root = _read_xml(zf, rels_path)
    if root is None:
        return {}
    rels: Dict[str, Tuple[str, str, str]] = {}
    for rel in root:
        if rel.tag.endswith("Relationship"):
            rid = rel.attrib.get("Id")
            target = rel.attrib.get("Target", "")
            rtype = rel.attrib.get("Type", "")
            mode = rel.attrib.get("TargetMode", "")
            if rid:
                rels[rid] = (target, rtype, mode)
    return rels


def _resolve_zip_path(base_dir: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(base_dir, target))


def _read_core_title(zf: zipfile.ZipFile) -> Optional[str]:
    root = _read_xml(zf, "docProps/core.xml")
    if root is None:
        return None
    el = root.find("dc:title", _NS)
    if el is not None and el.text and el.text.strip():
        return el.text.strip()
    return None


def _ordered_slide_paths(zf: zipfile.ZipFile) -> List[str]:
    """Return slide XML paths in exact presentation order."""
    pres = _read_xml(zf, "ppt/presentation.xml")
    rels = _read_rels(zf, "ppt/_rels/presentation.xml.rels")
    ordered: List[str] = []
    if pres is not None:
        for sld_id in pres.findall("p:sldIdLst/p:sldId", _NS):
            rid = sld_id.attrib.get(f"{_R}id")
            if rid and rid in rels:
                ordered.append(_resolve_zip_path("ppt", rels[rid][0]))
    if ordered:
        return ordered
    # Fallback: natural numeric sort over ppt/slides/slide*.xml
    slides = [n for n in zf.namelist() if re.match(r"^ppt/slides/slide\d+\.xml$", n)]
    return sorted(slides, key=lambda s: int(re.search(r"(\d+)\.xml$", s).group(1)))


def _shape_position(elem: ET.Element) -> Tuple[int, int]:
    off = elem.find(".//a:xfrm/a:off", _NS)
    if off is not None:
        try:
            return (int(off.attrib.get("y", "0")), int(off.attrib.get("x", "0")))
        except ValueError:
            pass
    return (0, 0)


def _placeholder_type(sp: ET.Element) -> str:
    ph = sp.find("p:nvSpPr/p:nvPr/p:ph", _NS)
    if ph is None:
        return ""
    return (ph.attrib.get("type") or "").strip().lower()


def _extract_paragraph_text(p: ET.Element, rels: Dict[str, Tuple[str, str, str]]) -> str:
    parts: List[str] = []
    for child in p:
        if child.tag == f"{_A}r":
            t_el = child.find("a:t", _NS)
            txt = t_el.text if (t_el is not None and t_el.text) else ""
            hlink = child.find("a:rPr/a:hlinkClick", _NS)
            if hlink is not None:
                rid = hlink.attrib.get(f"{_R}id")
                target = rels.get(rid or "", ("", "", ""))[0] if rid else ""
                if target.startswith(("http://", "https://")) and txt.strip() and txt.strip() != target:
                    parts.append(f"{txt} ({target})")
                    continue
            parts.append(txt)
        elif child.tag == f"{_A}fld":
            t_el = child.find("a:t", _NS)
            if t_el is not None and t_el.text:
                parts.append(t_el.text)
        elif child.tag == f"{_A}br":
            parts.append("\n")
    text = "".join(parts)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r" *\n *", "\n", text).strip()


def _paragraph_list_meta(p: ET.Element) -> Optional[Tuple[int, bool]]:
    ppr = p.find("a:pPr", _NS)
    if ppr is None:
        return None
    if ppr.find("a:buNone", _NS) is not None:
        return None
    try:
        lvl = int(ppr.attrib.get("lvl", "0"))
    except ValueError:
        lvl = 0
    if ppr.find("a:buAutoNum", _NS) is not None:
        return (max(0, lvl), True)
    if ppr.find("a:buChar", _NS) is not None or lvl > 0:
        return (max(0, lvl), False)
    return None


def _extract_table(tbl: ET.Element, rels: Dict[str, Tuple[str, str, str]]) -> List[List[str]]:
    rows: List[List[str]] = []
    for tr in tbl.findall("a:tr", _NS):
        row_cells: List[str] = []
        for tc in tr.findall("a:tc", _NS):
            if tc.attrib.get("hMerge") == "1" or tc.attrib.get("vMerge") == "1":
                row_cells.append("")
                continue
            para_texts = []
            for p in tc.findall("a:txBody/a:p", _NS):
                t = _extract_paragraph_text(p, rels)
                if t:
                    para_texts.append(t)
            row_cells.append("\n".join(para_texts))
        if any(c.strip() for c in row_cells):
            rows.append(row_cells)
    return rows


def _collect_shapes(sp_tree: ET.Element) -> List[ET.Element]:
    """Flatten shapes/tables/pictures (recursing into group shapes) in reading order."""
    collected: List[ET.Element] = []
    for child in sp_tree:
        if child.tag in {f"{_P}sp", f"{_P}graphicFrame", f"{_P}pic"}:
            collected.append(child)
        elif child.tag == f"{_P}grpSp":
            collected.extend(_collect_shapes(child))
    collected.sort(key=_shape_position)
    return collected


def _extract_notes(zf: zipfile.ZipFile, slide_path: str, slide_rels: Dict[str, Tuple[str, str, str]]) -> str:
    slide_dir = posixpath.dirname(slide_path)
    for target, rtype, _ in slide_rels.values():
        if rtype.endswith("/notesSlide"):
            notes_path = _resolve_zip_path(slide_dir, target)
            notes_root = _read_xml(zf, notes_path)
            if notes_root is None:
                return ""
            notes_rels_path = posixpath.join(
                posixpath.dirname(notes_path), "_rels", posixpath.basename(notes_path) + ".rels"
            )
            notes_rels = _read_rels(zf, notes_rels_path)
            note_lines: List[str] = []
            for sp in notes_root.findall(".//p:sp", _NS):
                ph_type = _placeholder_type(sp)
                if ph_type in {"sldnum", "sldimg", "hdr", "ftr", "dt"}:
                    continue
                for p in sp.findall("p:txBody/a:p", _NS):
                    t = _extract_paragraph_text(p, notes_rels)
                    if t:
                        note_lines.append(t)
            return "\n".join(note_lines).strip()
    return ""


def convert_pptx(path: Path, output_assets: Optional[Path] = None) -> Document:
    """Convert a .pptx PowerPoint presentation into a slide-structured Document."""
    if not zipfile.is_zipfile(path):
        raise RuntimeError(f"Not a valid .pptx archive: {path}")

    with zipfile.ZipFile(path, "r") as zf:
        slide_paths = _ordered_slide_paths(zf)
        if not slide_paths:
            raise RuntimeError(f"No slides found inside {path}")

        title = _read_core_title(zf) or path.stem
        document = Document(str(path), "pptx", title, metadata={"slides": len(slide_paths)})
        image_count = 0

        for idx, slide_path in enumerate(slide_paths, start=1):
            slide_root = _read_xml(zf, slide_path)
            if slide_root is None:
                continue
            slide_dir = posixpath.dirname(slide_path)
            rels_path = posixpath.join(slide_dir, "_rels", posixpath.basename(slide_path) + ".rels")
            slide_rels = _read_rels(zf, rels_path)

            sp_tree = slide_root.find(".//p:cSld/p:spTree", _NS)
            shapes = _collect_shapes(sp_tree) if sp_tree is not None else []

            slide_title: Optional[str] = None
            title_shape: Optional[ET.Element] = None
            for sp in shapes:
                if sp.tag == f"{_P}sp" and _placeholder_type(sp) in {"title", "ctrtitle"}:
                    texts = [
                        _extract_paragraph_text(p, slide_rels)
                        for p in sp.findall("p:txBody/a:p", _NS)
                    ]
                    joined = " ".join(t for t in texts if t).strip()
                    if joined:
                        slide_title = joined
                        title_shape = sp
                        break

            heading_text = f"Slide {idx}: {slide_title}" if slide_title else f"Slide {idx}"
            document.add(
                Block("heading", heading_text, source={"slide": idx}, metadata={"level": 2})
            )

            for shape in shapes:
                if shape is title_shape:
                    continue
                if shape.tag == f"{_P}sp":
                    ph_type = _placeholder_type(shape)
                    if ph_type in {"sldnum", "ftr", "dt"}:
                        continue
                    paragraphs = shape.findall("p:txBody/a:p", _NS)
                    list_lines: List[str] = []
                    list_counters: Dict[int, int] = {}

                    def flush_slide_list() -> None:
                        if list_lines:
                            document.add(
                                Block("list", "\n".join(list_lines), source={"slide": idx})
                            )
                            list_lines.clear()
                            list_counters.clear()

                    for p in paragraphs:
                        text = _extract_paragraph_text(p, slide_rels)
                        if not text:
                            continue
                        lmeta = _paragraph_list_meta(p)
                        # Body placeholder paragraphs are typically bullet items in PowerPoint
                        if lmeta is None and ph_type == "body" and len(paragraphs) > 1:
                            ppr = p.find("a:pPr", _NS)
                            if ppr is None or ppr.find("a:buNone", _NS) is None:
                                lmeta = (0, False)
                        if lmeta is not None:
                            lvl, is_ordered = lmeta
                            for k in list(list_counters.keys()):
                                if k > lvl:
                                    del list_counters[k]
                            indent = "  " * min(lvl, 4)
                            if is_ordered:
                                list_counters[lvl] = list_counters.get(lvl, 0) + 1
                                marker = f"{list_counters[lvl]}. "
                            else:
                                marker = "- "
                            list_lines.append(f"{indent}{marker}{text.replace(chr(10), ' ')}")
                        else:
                            flush_slide_list()
                            document.add(Block("paragraph", text, source={"slide": idx}))
                    flush_slide_list()

                elif shape.tag == f"{_P}graphicFrame":
                    for tbl in shape.findall(".//a:tbl", _NS):
                        rows = _extract_table(tbl, slide_rels)
                        if rows:
                            document.add(Block("table", rows=rows, source={"slide": idx}))

                elif shape.tag == f"{_P}pic" and output_assets is not None:
                    blip = shape.find(".//a:blip", _NS)
                    rid = blip.attrib.get(f"{_R}embed") if blip is not None else None
                    if rid and rid in slide_rels:
                        target, _, mode = slide_rels[rid]
                        if target and mode.lower() != "external":
                            zip_member = _resolve_zip_path(slide_dir, target)
                            try:
                                img_bytes = zf.read(zip_member)
                            except KeyError:
                                continue
                            output_assets.mkdir(parents=True, exist_ok=True)
                            image_count += 1
                            ext = Path(zip_member).suffix.lower() or ".png"
                            filename = f"slide-{idx:03d}-image-{image_count:02d}{ext}"
                            (output_assets / filename).write_bytes(img_bytes)
                            document.add(
                                Block(
                                    "image",
                                    filename,
                                    source={"slide": idx},
                                    metadata={"alt": f"Slide {idx} image"},
                                )
                            )

            notes_text = _extract_notes(zf, slide_path, slide_rels)
            if notes_text:
                document.add(
                    Block(
                        "heading",
                        "Speaker Notes",
                        source={"slide": idx, "section": "notes"},
                        metadata={"level": 3},
                    )
                )
                document.add(
                    Block(
                        "paragraph",
                        notes_text,
                        source={"slide": idx, "section": "notes"},
                    )
                )

            if idx < len(slide_paths):
                document.add(Block("page_break", source={"slide": idx}))

        document.metadata["exported_images"] = image_count
    return document
