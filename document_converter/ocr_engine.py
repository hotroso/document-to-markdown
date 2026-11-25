"""Shared PaddleOCR / PP-Structure helpers and language mapping."""

from __future__ import annotations

import io
import logging
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import Block

logger = logging.getLogger(__name__)

_paddle_ocr = None
_paddle_ocr_lang: Optional[str] = None
_structure_engine = None
_structure_lang: Optional[str] = None


def map_lang_for_paddle(lang: str) -> str:
    mapping = {
        "eng": "en",
        "en": "en",
        "vie": "vi",
        "vi": "vi",
        "chi_sim": "ch",
        "chi_tra": "chinese_cht",
        "ch": "ch",
        "jpn": "japan",
        "japan": "japan",
        "kor": "korean",
        "korean": "korean",
        "fra": "fr",
        "fr": "fr",
        "deu": "german",
        "german": "german",
        "spa": "es",
        "es": "es",
    }
    primary = (lang or "eng").split("+")[0].strip().lower()
    return mapping.get(primary, primary if primary in mapping.values() else "en")


def get_paddle_ocr(lang: str = "eng"):
    global _paddle_ocr, _paddle_ocr_lang
    mapped = map_lang_for_paddle(lang)
    if _paddle_ocr is not None and _paddle_ocr_lang == mapped:
        return _paddle_ocr
    from paddleocr import PaddleOCR

    try:
        _paddle_ocr = PaddleOCR(
            lang=mapped,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            show_log=False,
        )
    except TypeError:
        try:
            _paddle_ocr = PaddleOCR(use_angle_cls=True, lang=mapped, show_log=False, use_gpu=False)
        except TypeError:
            _paddle_ocr = PaddleOCR(use_angle_cls=True, lang=mapped)
    _paddle_ocr_lang = mapped
    logger.info("Loaded PaddleOCR (lang=%s)", mapped)
    return _paddle_ocr


def get_structure_engine(lang: str = "eng"):
    """Prefer PPStructureV3; fall back to classic PPStructure."""
    global _structure_engine, _structure_lang
    mapped = map_lang_for_paddle(lang)
    if _structure_engine is not None and _structure_lang == mapped:
        return _structure_engine

    # Try modern pipeline first
    try:
        from paddleocr import PPStructureV3

        try:
            _structure_engine = PPStructureV3(lang=mapped)
        except TypeError:
            _structure_engine = PPStructureV3()
        _structure_lang = mapped
        logger.info("Loaded PPStructureV3 (lang=%s)", mapped)
        return _structure_engine
    except Exception as exc:
        logger.debug("PPStructureV3 unavailable: %s", exc)

    try:
        from paddleocr import PPStructure

        try:
            _structure_engine = PPStructure(show_log=False, lang=mapped)
        except TypeError:
            try:
                _structure_engine = PPStructure(show_log=False)
            except TypeError:
                _structure_engine = PPStructure()
        _structure_lang = mapped
        logger.info("Loaded classic PPStructure")
        return _structure_engine
    except Exception as exc:
        raise RuntimeError(
            "PP-Structure requires a recent paddleocr with structure support. "
            "Install/update with: pip install -U paddleocr paddlepaddle"
        ) from exc


def _bbox_center_y(box) -> float:
    try:
        if isinstance(box, (list, tuple)) and len(box) >= 2:
            if isinstance(box[0], (list, tuple)):
                ys = [p[1] for p in box]
                return sum(ys) / len(ys)
            if len(box) >= 4:
                return (float(box[1]) + float(box[3])) / 2.0
    except Exception:
        pass
    return 0.0


def _bbox_center_x(box) -> float:
    try:
        if isinstance(box, (list, tuple)) and len(box) >= 2:
            if isinstance(box[0], (list, tuple)):
                xs = [p[0] for p in box]
                return sum(xs) / len(xs)
            if len(box) >= 4:
                return (float(box[0]) + float(box[2])) / 2.0
    except Exception:
        pass
    return 0.0


def _bbox_top(box) -> float:
    try:
        if isinstance(box, (list, tuple)) and len(box) >= 2:
            if isinstance(box[0], (list, tuple)):
                return min(p[1] for p in box)
            if len(box) >= 4:
                return float(box[1])
    except Exception:
        pass
    return 0.0


def run_plain_ocr_on_array(img_array, lang: str = "eng") -> List[Block]:
    """Plain PaddleOCR → ordered paragraph blocks (with multi-column sort)."""
    engine = get_paddle_ocr(lang)
    lines_data: List[Tuple[str, float, Any]] = []

    if hasattr(engine, "predict"):
        preds = engine.predict(img_array)
        if preds:
            res = preds[0]
            if hasattr(res, "rec_texts"):
                texts = list(res.rec_texts or [])
                scores = list(res.rec_scores or [0.9] * len(texts))
                boxes = list(getattr(res, "rec_boxes", None) or getattr(res, "dt_polys", None) or [])
            elif isinstance(res, dict):
                texts = res.get("rec_texts") or []
                scores = res.get("rec_scores") or [0.9] * len(texts)
                boxes = res.get("rec_boxes") or res.get("dt_polys") or []
            else:
                texts, scores, boxes = [], [], []
            lines_data = list(zip(texts, scores, boxes))
    else:
        ocr_out = engine.ocr(img_array, cls=True)
        if ocr_out and ocr_out[0]:
            for item in ocr_out[0]:
                if not item:
                    continue
                box, (text, score) = item[0], item[1]
                lines_data.append((text, float(score), box))

    if not lines_data:
        return []

    # Multi-column aware sort: cluster by x-center then by y
    width_hint = max((_bbox_center_x(b) for _, _, b in lines_data), default=1000.0) * 2
    col_threshold = max(80.0, width_hint * 0.25)

    def sort_key(item):
        text, score, box = item
        cx = _bbox_center_x(box)
        cy = _bbox_top(box)
        # Approximate column index
        col = int(cx // col_threshold)
        return (col, cy, cx)

    lines_data.sort(key=sort_key)

    blocks: List[Block] = []
    current: List[str] = []
    scores: List[float] = []
    last_y: Optional[float] = None
    last_col: Optional[int] = None
    y_threshold = 32.0

    for text, score, box in lines_data:
        text = (text or "").strip()
        if not text:
            continue
        cy = _bbox_top(box)
        cx = _bbox_center_x(box)
        col = int(cx // col_threshold)
        if (
            last_y is not None
            and last_col is not None
            and (col != last_col or abs(cy - last_y) > y_threshold)
            and current
        ):
            conf = sum(scores) / len(scores) if scores else None
            blocks.append(
                Block(
                    "paragraph",
                    " ".join(current),
                    confidence=conf,
                    metadata={"ocr_engine": "paddle"},
                )
            )
            current, scores = [], []
        current.append(text)
        try:
            scores.append(float(score))
        except Exception:
            scores.append(0.85)
        last_y, last_col = cy, col

    if current:
        conf = sum(scores) / len(scores) if scores else None
        blocks.append(
            Block(
                "paragraph",
                " ".join(current),
                confidence=conf,
                metadata={"ocr_engine": "paddle"},
            )
        )
    return blocks


def _html_table_to_rows(html: str) -> List[List[str]]:
    """Very lightweight HTML table → list of rows (no full parser dependency)."""
    import re

    rows: List[List[str]] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.I | re.S):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, flags=re.I | re.S)
        cleaned = []
        for cell in cells:
            text = re.sub(r"<[^>]+>", " ", cell)
            text = re.sub(r"\s+", " ", text).strip()
            cleaned.append(text)
        if cleaned:
            rows.append(cleaned)
    return rows


def structure_result_to_blocks(result: Any, page_number: Optional[int] = None) -> List[Block]:
    """Convert PPStructure / PPStructureV3 output into our Block list."""
    blocks: List[Block] = []
    source_base = {"page": page_number} if page_number is not None else {}

    # Modern PPStructureV3 often exposes markdown / structured regions
    if hasattr(result, "markdown") or (isinstance(result, dict) and "markdown" in result):
        md = getattr(result, "markdown", None) or (result.get("markdown") if isinstance(result, dict) else None)
        if md and isinstance(md, str) and md.strip():
            # Keep as a single rich block; renderer will still produce clean MD
            blocks.append(
                Block(
                    "paragraph",
                    md.strip(),
                    source=dict(source_base),
                    confidence=0.9,
                    metadata={"ocr_engine": "ppstructure", "format": "markdown"},
                )
            )
            return blocks

    # Classic list-of-dict layout
    items = result
    if isinstance(result, dict):
        items = result.get("res") or result.get("layout") or result.get("boxes") or [result]
    if not isinstance(items, list):
        items = [items]

    # Sort by reading order: top → bottom, left → right (multi-column friendly)
    def item_key(it):
        bbox = None
        if isinstance(it, dict):
            bbox = it.get("bbox") or it.get("coordinate") or it.get("box")
        elif hasattr(it, "bbox"):
            bbox = it.bbox
        if bbox is None:
            return (0.0, 0.0)
        try:
            if isinstance(bbox, (list, tuple)) and len(bbox) >= 4 and not isinstance(bbox[0], (list, tuple)):
                return (float(bbox[1]), float(bbox[0]))
            if isinstance(bbox, (list, tuple)) and bbox and isinstance(bbox[0], (list, tuple)):
                return (min(p[1] for p in bbox), min(p[0] for p in bbox))
        except Exception:
            pass
        return (0.0, 0.0)

    try:
        items = sorted(items, key=item_key)
    except Exception:
        pass

    for it in items:
        if not isinstance(it, dict):
            continue
        typ = (it.get("type") or it.get("label") or "text").lower()
        bbox = it.get("bbox") or it.get("coordinate")
        res = it.get("res")
        conf = it.get("score") or it.get("confidence")
        source = dict(source_base)
        if bbox is not None:
            try:
                if isinstance(bbox, (list, tuple)) and len(bbox) >= 4 and not isinstance(bbox[0], (list, tuple)):
                    source["bbox"] = [round(float(x), 1) for x in bbox[:4]]
            except Exception:
                pass

        # Table
        if "table" in typ:
            html = None
            if isinstance(res, dict):
                html = res.get("html")
            elif isinstance(res, str) and "<table" in res.lower():
                html = res
            rows = _html_table_to_rows(html) if html else []
            if rows:
                blocks.append(
                    Block(
                        "table",
                        rows=rows,
                        source=source,
                        confidence=float(conf) if conf is not None else 0.85,
                        metadata={"ocr_engine": "ppstructure", "detector": "structure"},
                    )
                )
                continue
            # Fallback: plain text of table region
            text = ""
            if isinstance(res, (list, tuple)):
                parts = []
                for line in res:
                    if isinstance(line, (list, tuple)) and len(line) >= 2:
                        if isinstance(line[1], (list, tuple)):
                            parts.append(str(line[1][0]))
                        else:
                            parts.append(str(line[1]))
                text = " ".join(parts)
            elif isinstance(res, str):
                text = res
            if text.strip():
                blocks.append(
                    Block(
                        "paragraph",
                        text.strip(),
                        source=source,
                        confidence=float(conf) if conf is not None else 0.7,
                        metadata={"ocr_engine": "ppstructure", "note": "table_as_text"},
                    )
                )
            continue

        # Title / heading
        if any(k in typ for k in ("title", "heading", "header", "doc_title", "paragraph_title")):
            text = _extract_text_from_res(res)
            if text:
                level = 1 if "doc_title" in typ or typ == "title" else 2
                blocks.append(
                    Block(
                        "heading",
                        text,
                        source=source,
                        confidence=float(conf) if conf is not None else 0.88,
                        metadata={"level": level, "ocr_engine": "ppstructure"},
                    )
                )
            continue

        # Image / figure – skip text or mark as image placeholder
        if any(k in typ for k in ("figure", "image", "chart", "seal")):
            continue

        # Footer / header / page number – lower priority, still keep as paragraph
        text = _extract_text_from_res(res)
        if text:
            kind = "paragraph"
            if _looks_like_list(text):
                kind = "list"
            blocks.append(
                Block(
                    kind,
                    text,
                    source=source,
                    confidence=float(conf) if conf is not None else 0.8,
                    metadata={"ocr_engine": "ppstructure", "layout_type": typ},
                )
            )

    return blocks


def _extract_text_from_res(res) -> str:
    if res is None:
        return ""
    if isinstance(res, str):
        return res.strip()
    if isinstance(res, (list, tuple)):
        parts = []
        for line in res:
            if isinstance(line, (list, tuple)):
                if len(line) >= 2 and isinstance(line[1], (list, tuple)):
                    parts.append(str(line[1][0]))
                elif len(line) >= 2:
                    parts.append(str(line[1]))
                elif len(line) == 1:
                    parts.append(str(line[0]))
            else:
                parts.append(str(line))
        return " ".join(p for p in parts if p).strip()
    if isinstance(res, dict):
        for key in ("text", "rec_text", "content"):
            if key in res:
                return str(res[key]).strip()
    return str(res).strip()


def _looks_like_list(text: str) -> bool:
    import re

    return any(
        re.match(r"^\s*(?:[•●▪◦·-]|\d+[.)]|[A-Za-z][.)])\s+", line)
        for line in text.splitlines()
    )


def run_structure_on_image(img_array_or_path, lang: str = "eng", page_number: Optional[int] = None) -> List[Block]:
    """Full layout + table + reading-order analysis."""
    engine = get_structure_engine(lang)

    # PPStructureV3
    if hasattr(engine, "predict"):
        outputs = engine.predict(img_array_or_path)
        blocks: List[Block] = []
        for res in outputs or []:
            blocks.extend(structure_result_to_blocks(res, page_number=page_number))
        if blocks:
            return blocks

    # Classic PPStructure callable
    if callable(engine):
        result = engine(img_array_or_path)
        return structure_result_to_blocks(result, page_number=page_number)

    return []
