"""Layout-aware PDF adapter with pdfplumber/pypdfium2 (permissive), PP-Structure & PaddleOCR."""

from __future__ import annotations

import logging
import re
import tempfile
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

from .models import Block, Document
from .ocr_engine import (
    detect_ocr_engine,
    ocr_pil_image_tesseract,
    run_plain_ocr_on_array,
    run_structure_on_image,
)

logger = logging.getLogger(__name__)


def _normal(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _looks_like_list(text: str) -> bool:
    return any(
        re.match(r"^\s*(?:[•●▪◦·-]|\d+[.)]|[A-Za-z][.)])\s+", line)
        for line in text.splitlines()
    )


def _table_rows(lines: List[str]) -> Optional[List[List[str]]]:
    if len(lines) < 2:
        return None
    rows = []
    counts = []
    for line in lines:
        parts = [part.strip() for part in re.split(r"\t|\s{2,}", line.strip()) if part.strip()]
        rows.append(parts)
        counts.append(len(parts))
    if min(counts) < 2 or max(counts) != min(counts) or len(set(counts)) != 1:
        return None
    return rows


def _cluster_columns(blocks_raw: List[dict], page_width: float) -> List[List[dict]]:
    """Group native PDF blocks into columns by x-center for better reading order."""
    if not blocks_raw or page_width <= 0:
        return [blocks_raw]

    centers = []
    for b in blocks_raw:
        bbox = b.get("bbox")
        if not bbox or len(bbox) < 4:
            centers.append(page_width / 2)
        else:
            centers.append((bbox[0] + bbox[2]) / 2)

    # Simple 1-D clustering: gap > 15% of page width → new column
    gap_threshold = page_width * 0.15
    indexed = sorted(enumerate(centers), key=lambda x: x[1])
    columns: List[List[int]] = []
    current: List[int] = []
    last_c = None
    for idx, c in indexed:
        if last_c is not None and (c - last_c) > gap_threshold and current:
            columns.append(current)
            current = []
        current.append(idx)
        last_c = c
    if current:
        columns.append(current)

    if len(columns) <= 1:
        return [blocks_raw]

    # Within each column sort by y (top), then emit columns left-to-right
    result_cols: List[List[dict]] = []
    for col_indices in columns:
        col_blocks = [blocks_raw[i] for i in col_indices]
        col_blocks.sort(key=lambda b: (b.get("bbox") or [0, 0])[1])
        result_cols.append(col_blocks)
    return result_cols


def _word_inside_any(word: dict, bboxes: List[Tuple[float, float, float, float]]) -> bool:
    if not bboxes:
        return False
    cx = (float(word["x0"]) + float(word["x1"])) / 2.0
    cy = (float(word["top"]) + float(word["bottom"])) / 2.0
    for x0, y0, x1, y1 in bboxes:
        if x0 - 1.0 <= cx <= x1 + 1.0 and y0 - 1.0 <= cy <= y1 + 1.0:
            return True
    return False


def _clean_extracted_table(raw_rows: List[List[Optional[str]]]) -> List[List[str]]:
    cleaned: List[List[str]] = []
    for row in raw_rows or []:
        cells = [re.sub(r"\s+", " ", (c or "")).strip() for c in row]
        if any(cells):
            cleaned.append(cells)
    if len(cleaned) < 2 or max((len(r) for r in cleaned), default=0) < 2:
        return []
    return cleaned


def _page_blocks(page, page_number: int) -> List[Block]:
    """Extract tables, headings, lists, and paragraphs from a pdfplumber page."""
    page_width = float(getattr(page, "width", 0) or 0)

    # 1. Detect ruled tables first so their words are not duplicated as paragraphs
    table_entries: List[dict] = []
    table_bboxes: List[Tuple[float, float, float, float]] = []
    try:
        for tbl in page.find_tables() or []:
            rows = _clean_extracted_table(tbl.extract())
            if rows:
                bbox = tuple(float(v) for v in tbl.bbox)
                table_bboxes.append(bbox)
                table_entries.append(
                    {
                        "kind": "table",
                        "rows": rows,
                        "bbox": list(bbox),
                    }
                )
    except Exception:
        pass

    # 2. Extract words with font sizes and group into lines and paragraphs
    try:
        words = page.extract_words(extra_attrs=["size"], keep_blank_chars=False) or []
    except TypeError:
        words = page.extract_words() or []

    words = [w for w in words if str(w.get("text", "")).strip() and not _word_inside_any(w, table_bboxes)]
    sizes = [float(w.get("size", 0) or 0) for w in words if float(w.get("size", 0) or 0) > 0]
    body_size = median(sizes) if sizes else 11.0

    # Group words into visual lines
    heights = [
        max(1.0, float(w.get("bottom", 0)) - float(w.get("top", 0)))
        for w in words
        if float(w.get("bottom", 0)) > float(w.get("top", 0))
    ]
    med_h = median(heights) if heights else 11.0
    line_tol = max(3.0, med_h * 0.5)
    para_gap = max(8.0, med_h * 1.25)

    words_sorted = sorted(words, key=lambda w: ((float(w["top"]) + float(w["bottom"])) / 2.0, float(w["x0"])))
    visual_lines: List[List[dict]] = []
    for w in words_sorted:
        cy = (float(w["top"]) + float(w["bottom"])) / 2.0
        if visual_lines:
            prev_cy = sum((float(x["top"]) + float(x["bottom"])) / 2.0 for x in visual_lines[-1]) / len(visual_lines[-1])
            if abs(cy - prev_cy) <= line_tol:
                visual_lines[-1].append(w)
                continue
        visual_lines.append([w])

    # Split visual lines by wide horizontal gaps (multi-column lines) before grouping into paragraphs
    col_gap = max(25.0, page_width * 0.08) if page_width > 0 else 40.0
    line_segments: List[dict] = []
    for vline in visual_lines:
        vline.sort(key=lambda w: float(w["x0"]))
        seg: List[dict] = []
        for w in vline:
            if seg and (float(w["x0"]) - float(seg[-1]["x1"])) > col_gap:
                line_segments.append(_make_line_segment(seg, body_size))
                seg = [w]
            else:
                seg.append(w)
        if seg:
            line_segments.append(_make_line_segment(seg, body_size))

    # Cluster line segments into columns, then merge adjacent lines into paragraphs
    ordered_cols = _cluster_columns(line_segments, page_width)
    raw_blocks: List[dict] = []
    for col_lines in ordered_cols:
        col_lines.sort(key=lambda item: (item["bbox"][1], item["bbox"][0]))
        cur_lines: List[str] = []
        cur_sizes: List[float] = []
        cur_bbox: Optional[List[float]] = None
        prev_bottom: Optional[float] = None
        prev_size: Optional[float] = None

        def flush_para() -> None:
            nonlocal cur_lines, cur_sizes, cur_bbox
            if cur_lines and cur_bbox is not None:
                raw_blocks.append(
                    {
                        "kind": "text",
                        "lines": list(cur_lines),
                        "line_sizes": list(cur_sizes),
                        "bbox": list(cur_bbox),
                        "text": "\n".join(cur_lines),
                    }
                )
            cur_lines, cur_sizes, cur_bbox = [], [], None

        for seg in col_lines:
            top, bottom = seg["bbox"][1], seg["bbox"][3]
            seg_size = median(seg["line_sizes"]) if seg["line_sizes"] else body_size
            size_jump = prev_size is not None and abs(seg_size - prev_size) >= max(2.0, body_size * 0.2)
            if (
                prev_bottom is not None
                and ((top - prev_bottom) > para_gap or size_jump)
                and cur_lines
            ):
                flush_para()
            cur_lines.append(seg["text"])
            cur_sizes.extend(seg["line_sizes"])
            if cur_bbox is None:
                cur_bbox = list(seg["bbox"])
            else:
                cur_bbox = [
                    min(cur_bbox[0], seg["bbox"][0]),
                    min(cur_bbox[1], seg["bbox"][1]),
                    max(cur_bbox[2], seg["bbox"][2]),
                    max(cur_bbox[3], seg["bbox"][3]),
                ]
            prev_bottom = bottom
            prev_size = seg_size
        flush_para()

    # Merge text blocks and ruled tables in reading order
    all_items = raw_blocks + table_entries
    ordered_groups = _cluster_columns(all_items, page_width)
    ordered = [b for group in ordered_groups for b in group]

    blocks: List[Block] = []
    for item in ordered:
        source: Dict[str, Any] = {"page": page_number}
        if item.get("bbox"):
            source["bbox"] = [round(float(v), 1) for v in item["bbox"]]

        if item.get("kind") == "table":
            blocks.append(
                Block(
                    "table",
                    rows=item["rows"],
                    source=source,
                    confidence=0.92,
                    metadata={"detector": "pdfplumber"},
                )
            )
            continue

        lines = item["lines"]
        text = item["text"]

        table = _table_rows(lines)
        if table:
            blocks.append(
                Block(
                    "table",
                    rows=table,
                    source=source,
                    confidence=0.68,
                    metadata={"detector": "whitespace"},
                )
            )
            continue

        average_size = median(item["line_sizes"]) if item["line_sizes"] else body_size
        caps = text == text.upper() and any(char.isalpha() for char in text)
        if len(lines) <= 2 and (average_size >= body_size * 1.22 or (caps and len(text) <= 120)):
            blocks.append(
                Block(
                    "heading",
                    " ".join(lines),
                    source=source,
                    confidence=0.74,
                    metadata={"level": 2, "font_size": average_size},
                )
            )
        elif _looks_like_list(text):
            blocks.append(Block("list", text, source=source, confidence=0.72))
        else:
            blocks.append(Block("paragraph", " ".join(lines), source=source, confidence=0.86))
    return blocks


def _make_line_segment(words: List[dict], default_size: float) -> dict:
    text = " ".join(str(w["text"]).strip() for w in words if str(w.get("text", "")).strip())
    sizes = [float(w.get("size", 0) or default_size) for w in words]
    bbox = [
        min(float(w["x0"]) for w in words),
        min(float(w["top"]) for w in words),
        max(float(w["x1"]) for w in words),
        max(float(w["bottom"]) for w in words),
    ]
    return {"text": text, "line_sizes": sizes, "bbox": bbox}


def _render_page_pil(pdfium_doc, page_index: int, dpi: int):
    """Render one PDF page to an RGB PIL Image using pypdfium2 (Apache-2.0 / BSD)."""
    page = pdfium_doc[page_index]
    try:
        bitmap = page.render(scale=dpi / 72.0)
        try:
            return bitmap.to_pil().convert("RGB")
        finally:
            if hasattr(bitmap, "close"):
                bitmap.close()
    finally:
        if hasattr(page, "close"):
            page.close()


def _ocr_page_with_structure(
    pdfium_doc,
    page_index: int,
    page_number: int,
    lang: str,
    ocr_version: Optional[str] = None,
    ocr_profile: str = "auto",
    det_model: Optional[str] = None,
    rec_model: Optional[str] = None,
) -> List[Block]:
    """Render page and run PP-Structure for layout + tables + reading order."""
    image = _render_page_pil(pdfium_doc, page_index, dpi=220)
    model_kw = {
        "ocr_version": ocr_version,
        "ocr_profile": ocr_profile,
        "det_model": det_model,
        "rec_model": rec_model,
    }
    try:
        import numpy as np

        img_array = np.array(image)
        return run_structure_on_image(img_array, lang=lang, page_number=page_number, **model_kw)
    except Exception:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            image.save(tmp_path, format="PNG")
            return run_structure_on_image(tmp_path, lang=lang, page_number=page_number, **model_kw)
        finally:
            Path(tmp_path).unlink(missing_ok=True)


def _ocr_page_plain(
    pdfium_doc,
    page_index: int,
    page_number: int,
    lang: str,
    ocr_version: Optional[str] = None,
    ocr_profile: str = "auto",
    det_model: Optional[str] = None,
    rec_model: Optional[str] = None,
) -> List[Block]:
    image = _render_page_pil(pdfium_doc, page_index, dpi=250)
    model_kw = {
        "ocr_version": ocr_version,
        "ocr_profile": ocr_profile,
        "det_model": det_model,
        "rec_model": rec_model,
    }
    import numpy as np

    blocks = run_plain_ocr_on_array(np.array(image), lang=lang, **model_kw)
    for b in blocks:
        b.source = {**b.source, "page": page_number}
    return blocks


def _ocr_page_tesseract(pdfium_doc, page_index: int, page_number: int, lang: str) -> List[Block]:
    image = _render_page_pil(pdfium_doc, page_index, dpi=300)
    return ocr_pil_image_tesseract(image, lang=lang, page_number=page_number)


def _page_needs_ocr(page, native_text: str) -> bool:
    """Detect scanned or image-dominated PDF pages that need OCR."""
    images = getattr(page, "images", None) or []
    if not images:
        return False
    if len(native_text) < 40:
        return True
    if len(native_text) >= 250:
        return False
    page_area = float(getattr(page, "width", 0) or 0) * float(getattr(page, "height", 0) or 0)
    if page_area <= 0:
        return False
    image_area = 0.0
    for info in images:
        try:
            if isinstance(info, dict):
                w = float(info.get("width", float(info.get("x1", 0)) - float(info.get("x0", 0))))
                h = float(info.get("height", float(info.get("bottom", 0)) - float(info.get("top", 0))))
                image_area += max(0.0, w * h)
        except Exception:
            continue
    return (image_area / page_area) >= 0.35


def _export_page_images(
    page,
    pdfium_doc,
    page_index: int,
    page_number: int,
    output_assets: Path,
    document: Document,
) -> List[Block]:
    images = getattr(page, "images", None) or []
    if not images or pdfium_doc is None:
        return []
    dpi = 200
    scale = dpi / 72.0
    rendered = None
    image_blocks: List[Block] = []
    output_assets.mkdir(parents=True, exist_ok=True)
    for image_index, info in enumerate(images, start=1):
        try:
            if rendered is None:
                rendered = _render_page_pil(pdfium_doc, page_index, dpi=dpi)
            rw, rh = rendered.size
            x0 = max(0, min(rw, int(round(float(info.get("x0", 0)) * scale))))
            y0 = max(0, min(rh, int(round(float(info.get("top", 0)) * scale))))
            x1 = max(0, min(rw, int(round(float(info.get("x1", 0)) * scale))))
            y1 = max(0, min(rh, int(round(float(info.get("bottom", 0)) * scale))))
            if (x1 - x0) < 8 or (y1 - y0) < 8:
                continue
            filename = output_assets / f"page-{page_number:03d}-image-{image_index:02d}.png"
            rendered.crop((x0, y0, x1, y1)).save(str(filename), format="PNG")
            image_blocks.append(
                Block(
                    "image",
                    filename.name,
                    source={"page": page_number},
                    metadata={"alt": f"Page {page_number} image"},
                )
            )
        except Exception as exc:
            document.warn(f"Page {page_number}: image export failed ({exc})")
    return image_blocks


def _remove_repeated_edges(pages: List[List[Block]], document: Document) -> None:
    if len(pages) < 2:
        return
    for position, label in ((0, "header"), (-1, "footer")):
        candidates = [
            _normal(page[position].text)
            for page in pages
            if page and page[position].kind in {"paragraph", "heading"}
        ]
        if not candidates:
            continue
        value, count = Counter(candidates).most_common(1)[0]
        if count < max(2, int(len(pages) * 0.6)) or len(value) > 160:
            continue
        removed = 0
        for page in pages:
            if page and _normal(page[position].text) == value:
                page.pop(position)
                removed += 1
        document.metadata[f"removed_{label}"] = value
        if removed:
            document.warn(f"Repeated {label} removed from {removed} pages: {value}")


def convert_pdf(
    path: Path,
    output_assets: Optional[Path] = None,
    ocr: str = "auto",
    ocr_lang: str = "vie",
    preview: bool = False,
    password: Optional[str] = None,
    use_structure: bool = True,
    ocr_version: Optional[str] = None,
    ocr_profile: str = "auto",
    det_model: Optional[str] = None,
    rec_model: Optional[str] = None,
) -> Document:
    try:
        import pdfplumber
    except ImportError as exc:
        raise RuntimeError(
            "PDF support requires pdfplumber. Install it with: pip install pdfplumber"
        ) from exc

    engine = detect_ocr_engine(ocr)
    model_kw = {
        "ocr_version": ocr_version,
        "ocr_profile": ocr_profile,
        "det_model": det_model,
        "rec_model": rec_model,
    }

    try:
        pdf = pdfplumber.open(str(path), password=password)
    except Exception as exc:
        name = type(exc).__name__.lower()
        if "password" in name or "password" in str(exc).lower() or "encrypt" in str(exc).lower():
            raise RuntimeError("Password required or incorrect for this PDF") from exc
        raise

    pdfium_doc = None

    def get_pdfium():
        nonlocal pdfium_doc
        if pdfium_doc is None:
            try:
                import pypdfium2
            except ImportError as exc:
                raise RuntimeError(
                    "PDF rendering for OCR/image export requires pypdfium2. "
                    "Install it with: pip install pypdfium2"
                ) from exc
            pdfium_doc = pypdfium2.PdfDocument(str(path), password=password)
        return pdfium_doc

    try:
        total_pages = len(pdf.pages)
        meta: Dict[str, Any] = {
            "pages": total_pages,
            "ocr_mode": ocr,
            "ocr_engine": engine,
            "use_structure": use_structure,
        }
        if (ocr_profile or "auto").lower() != "auto":
            meta["ocr_profile"] = ocr_profile
        if ocr_version:
            meta["ocr_version"] = ocr_version
        if det_model:
            meta["det_model"] = det_model
        if rec_model:
            meta["rec_model"] = rec_model
        document = Document(
            str(path),
            "pdf",
            path.stem,
            metadata=meta,
        )
        page_count = min(total_pages, 3) if preview else total_pages
        page_blocks: List[List[Block]] = []

        for index in range(page_count):
            page = pdf.pages[index]
            native = _page_blocks(page, index + 1)
            native_text = " ".join(block.text for block in native).strip()
            use_ocr = engine != "off" and (
                ocr in {"paddle", "tesseract"}
                or (ocr == "auto" and _page_needs_ocr(page, native_text))
            )
            blocks = native

            if use_ocr:
                try:
                    pdoc = get_pdfium()
                    if engine == "paddle" and use_structure:
                        try:
                            blocks = _ocr_page_with_structure(
                                pdoc, index, index + 1, ocr_lang, **model_kw
                            )
                            if blocks:
                                document.metadata["ocr_engine"] = "ppstructure"
                            else:
                                document.warn(
                                    f"Page {index + 1}: structure returned empty; trying plain OCR."
                                )
                                blocks = _ocr_page_plain(
                                    pdoc, index, index + 1, ocr_lang, **model_kw
                                )
                        except Exception as se:
                            document.warn(
                                f"Page {index + 1}: structure failed ({se}); plain OCR fallback."
                            )
                            blocks = _ocr_page_plain(
                                pdoc, index, index + 1, ocr_lang, **model_kw
                            )
                    elif engine == "paddle":
                        blocks = _ocr_page_plain(pdoc, index, index + 1, ocr_lang, **model_kw)
                    else:
                        blocks = _ocr_page_tesseract(pdoc, index, index + 1, ocr_lang)

                    if not blocks:
                        document.warn(
                            f"Page {index + 1}: OCR returned no text; using native extraction."
                        )
                        blocks = native
                except Exception as exc:
                    document.warn(
                        f"Page {index + 1}: OCR unavailable or failed ({exc}); native text retained."
                    )
                    blocks = native

            image_blocks: List[Block] = []
            if output_assets is not None and (getattr(page, "images", None) or []):
                try:
                    image_blocks = _export_page_images(
                        page, get_pdfium(), index, index + 1, output_assets, document
                    )
                except Exception as exc:
                    document.warn(f"Page {index + 1}: image export failed ({exc})")
            page_blocks.append(blocks + image_blocks)

        _remove_repeated_edges(page_blocks, document)
        for index, blocks in enumerate(page_blocks):
            document.blocks.extend(blocks)
            if index < len(page_blocks) - 1:
                document.add(Block("page_break", source={"page": index + 1}))
        document.metadata["processed_pages"] = page_count
        return document
    finally:
        pdf.close()
        if pdfium_doc is not None and hasattr(pdfium_doc, "close"):
            pdfium_doc.close()
