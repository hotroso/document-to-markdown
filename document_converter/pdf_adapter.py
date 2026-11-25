"""Layout-aware PDF adapter with native extraction, PP-Structure and PaddleOCR."""

from __future__ import annotations

import io
import logging
import re
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


def _page_blocks(page, page_number: int) -> List[Block]:
    data = page.get_text("dict", sort=True)
    page_width = float(page.rect.width)
    sizes = [
        float(span.get("size", 0) or 0)
        for raw in data.get("blocks", [])
        for line in raw.get("lines", [])
        for span in line.get("spans", [])
        if span.get("text", "").strip()
    ]
    body_size = median(sizes) if sizes else 11.0

    raw_blocks: List[dict] = []
    for raw in data.get("blocks", []):
        if "lines" not in raw:
            continue
        lines: List[str] = []
        line_sizes: List[float] = []
        for line in raw.get("lines", []):
            text = "".join(str(span.get("text", "")) for span in line.get("spans", []))
            if text.strip():
                lines.append(text.strip())
                line_sizes.extend(
                    float(span.get("size", 0) or 0)
                    for span in line.get("spans", [])
                    if span.get("text", "").strip()
                )
        if not lines:
            continue
        raw_blocks.append(
            {
                "lines": lines,
                "line_sizes": line_sizes,
                "bbox": list(raw.get("bbox") or []),
                "text": "\n".join(lines),
            }
        )

    # Multi-column aware ordering
    ordered_groups = _cluster_columns(raw_blocks, page_width)
    ordered = [b for group in ordered_groups for b in group]

    blocks: List[Block] = []
    for item in ordered:
        lines = item["lines"]
        text = item["text"]
        source = {"page": page_number}
        if item["bbox"]:
            source["bbox"] = [round(float(v), 1) for v in item["bbox"]]

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


def _ocr_page_with_structure(page, page_number: int, lang: str) -> List[Block]:
    """Render page and run PP-Structure for layout + tables + reading order."""
    pix = page.get_pixmap(dpi=220)
    img_bytes = pix.tobytes("png")
    try:
        import numpy as np
        from PIL import Image

        image = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img_array = np.array(image)
        return run_structure_on_image(img_array, lang=lang, page_number=page_number)
    except Exception:
        # path fallback
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(img_bytes)
            tmp_path = tmp.name
        try:
            return run_structure_on_image(tmp_path, lang=lang, page_number=page_number)
        finally:
            Path(tmp_path).unlink(missing_ok=True)


def _ocr_page_plain(page, page_number: int, lang: str) -> List[Block]:
    pix = page.get_pixmap(dpi=250)
    img_bytes = pix.tobytes("png")
    try:
        import numpy as np
        from PIL import Image

        image = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img_array = np.array(image)
        blocks = run_plain_ocr_on_array(img_array, lang=lang)
        for b in blocks:
            b.source = {"page": page_number}
        return blocks
    except Exception:
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(img_bytes)
            tmp_path = tmp.name
        try:
            from PIL import Image
            import numpy as np

            image = Image.open(tmp_path).convert("RGB")
            blocks = run_plain_ocr_on_array(np.array(image), lang=lang)
            for b in blocks:
                b.source = {"page": page_number}
            return blocks
        finally:
            Path(tmp_path).unlink(missing_ok=True)


def _ocr_page_tesseract(page, page_number: int, lang: str) -> List[Block]:
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Tesseract OCR requires pytesseract and Pillow") from exc

    image = Image.open(io.BytesIO(page.get_pixmap(dpi=300).tobytes("png")))
    return ocr_pil_image_tesseract(image, lang=lang, page_number=page_number)


def _page_needs_ocr(page, native_text: str) -> bool:
    """Detect scanned or image-dominated PDF pages that need OCR."""
    images = page.get_images(full=True)
    if not images:
        return False
    if len(native_text) < 40:
        return True
    if len(native_text) >= 250:
        return False
    page_area = float(page.rect.width * page.rect.height)
    if page_area <= 0:
        return False
    image_area = 0.0
    for info in images:
        try:
            for rect in page.get_image_rects(info[0]):
                image_area += max(0.0, float(rect.width * rect.height))
        except Exception:
            continue
    return (image_area / page_area) >= 0.35


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
) -> Document:
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PDF support requires PyMuPDF. Install it with: pip install pymupdf") from exc

    engine = detect_ocr_engine(ocr)
    pdf = fitz.open(path)
    try:
        if pdf.needs_pass:
            if not password or not pdf.authenticate(password):
                raise RuntimeError("Password required or incorrect for this PDF")
        document = Document(
            str(path),
            "pdf",
            path.stem,
            metadata={
                "pages": pdf.page_count,
                "ocr_mode": ocr,
                "ocr_engine": engine,
                "use_structure": use_structure,
            },
        )
        page_count = min(pdf.page_count, 3) if preview else pdf.page_count
        page_blocks: List[List[Block]] = []

        for index in range(page_count):
            page = pdf.load_page(index)
            native = _page_blocks(page, index + 1)
            native_text = " ".join(block.text for block in native).strip()
            use_ocr = engine != "off" and (
                ocr in {"paddle", "tesseract"}
                or (ocr == "auto" and _page_needs_ocr(page, native_text))
            )
            blocks = native

            if use_ocr:
                try:
                    if engine == "paddle" and use_structure:
                        try:
                            blocks = _ocr_page_with_structure(page, index + 1, ocr_lang)
                            if blocks:
                                document.metadata["ocr_engine"] = "ppstructure"
                            else:
                                document.warn(
                                    f"Page {index + 1}: structure returned empty; trying plain OCR."
                                )
                                blocks = _ocr_page_plain(page, index + 1, ocr_lang)
                        except Exception as se:
                            document.warn(
                                f"Page {index + 1}: structure failed ({se}); plain OCR fallback."
                            )
                            blocks = _ocr_page_plain(page, index + 1, ocr_lang)
                    elif engine == "paddle":
                        blocks = _ocr_page_plain(page, index + 1, ocr_lang)
                    else:
                        blocks = _ocr_page_tesseract(page, index + 1, ocr_lang)

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
            if output_assets is not None:
                output_assets.mkdir(parents=True, exist_ok=True)
                for image_index, image_info in enumerate(page.get_images(full=True), start=1):
                    try:
                        pix = fitz.Pixmap(pdf, image_info[0])
                        if pix.n > 4:
                            pix = fitz.Pixmap(fitz.csRGB, pix)
                        filename = output_assets / f"page-{index + 1:03d}-image-{image_index:02d}.png"
                        pix.save(str(filename))
                        image_blocks.append(
                            Block(
                                "image",
                                filename.name,
                                source={"page": index + 1},
                                metadata={"alt": f"Page {index + 1} image"},
                            )
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
