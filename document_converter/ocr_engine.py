"""Shared PaddleOCR / PP-Structure helpers and language mapping."""

from __future__ import annotations

import inspect
import io
import logging
import re
import tempfile
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

from .models import Block

logger = logging.getLogger(__name__)

_paddle_ocr = None
_paddle_ocr_lang: Optional[str] = None
_structure_engine = None
_structure_lang: Optional[str] = None

_PADDLE_LANG_MAP = {
    "eng": "en",
    "en": "en",
    "vie": "vi",
    "vi": "vi",
    "chi_sim": "ch",
    "zh": "ch",
    "zh_cn": "ch",
    "ch": "ch",
    "chi_tra": "chinese_cht",
    "zh_tw": "chinese_cht",
    "chinese_cht": "chinese_cht",
    "jpn": "japan",
    "ja": "japan",
    "japan": "japan",
    "kor": "korean",
    "ko": "korean",
    "korean": "korean",
    "fra": "fr",
    "fr": "fr",
    "deu": "german",
    "de": "german",
    "german": "german",
    "spa": "es",
    "es": "es",
    "ita": "it",
    "it": "it",
    "por": "pt",
    "pt": "pt",
    "rus": "ru",
    "ru": "ru",
    "tha": "th",
    "th": "th",
    "latin": "latin",
}

_TESSERACT_LANG_MAP = {
    "en": "eng",
    "vi": "vie",
    "ch": "chi_sim",
    "zh": "chi_sim",
    "zh_cn": "chi_sim",
    "chinese_cht": "chi_tra",
    "zh_tw": "chi_tra",
    "japan": "jpn",
    "ja": "jpn",
    "korean": "kor",
    "ko": "kor",
    "fr": "fra",
    "german": "deu",
    "de": "deu",
    "es": "spa",
    "it": "ita",
    "pt": "por",
    "ru": "rus",
    "th": "tha",
}


def _split_lang_tokens(lang: str) -> List[str]:
    return [part.strip().lower() for part in re.split(r"[+,]", lang or "vie") if part.strip()]


def map_lang_for_paddle(lang: str) -> str:
    """Pick the best single PaddleOCR model covering requested language(s)."""
    tokens = _split_lang_tokens(lang)
    if not tokens:
        return "vi"
    mapped = [_PADDLE_LANG_MAP.get(tok, tok if tok in _PADDLE_LANG_MAP.values() else "en") for tok in tokens]
    # Non-English PaddleOCR models (vi, ch, japan, korean, ...) already include ASCII/English.
    for code in mapped:
        if code != "en":
            return code
    return mapped[0]


def map_lang_for_tesseract(lang: str) -> str:
    """Normalize language specification (e.g. 'vi', 'eng+vie') for Tesseract."""
    tokens = _split_lang_tokens(lang)
    if not tokens:
        return "vie"
    mapped = [_TESSERACT_LANG_MAP.get(tok, tok) for tok in tokens]
    return "+".join(dict.fromkeys(mapped))


def detect_ocr_engine(requested: str) -> str:
    """Resolve requested OCR engine ('auto' prefers PaddleOCR, falls back to Tesseract)."""
    requested = (requested or "auto").lower()
    if requested in {"off", "none"}:
        return "off"
    if requested == "tesseract":
        return "tesseract"
    if requested == "paddle":
        return "paddle"
    try:
        import paddleocr  # noqa: F401
        return "paddle"
    except ImportError:
        pass
    try:
        import pytesseract  # noqa: F401
        return "tesseract"
    except ImportError:
        return "off"


def _parse_major_version(version: Any) -> Optional[int]:
    if not isinstance(version, str):
        return None
    match = re.match(r"^\s*(\d+)", version)
    return int(match.group(1)) if match else None


def _is_paddleocr_v3(paddleocr_module: Any, paddle_cls: Any) -> bool:
    """Return True for PaddleOCR 3.x+ (modern API that removed show_log)."""
    major = _parse_major_version(getattr(paddleocr_module, "__version__", None))
    if major is not None:
        return major >= 3
    if hasattr(paddle_cls, "predict"):
        return True
    try:
        params = inspect.signature(paddle_cls).parameters
        if "use_doc_orientation_classify" in params:
            return True
    except (TypeError, ValueError):
        pass
    return False


def _silence_paddle_logger(paddleocr_module: Any) -> None:
    """Suppress verbose PaddleOCR 3.x logs via Python logging unless debug mode is active."""
    if logger.isEnabledFor(logging.DEBUG):
        return
    paddle_logger = getattr(paddleocr_module, "logger", None)
    if isinstance(paddle_logger, logging.Logger) and paddle_logger.level in (
        logging.NOTSET,
        logging.DEBUG,
        logging.INFO,
    ):
        paddle_logger.setLevel(logging.WARNING)
    for name in ("ppocr", "paddleocr"):
        mod_logger = logging.getLogger(name)
        if mod_logger.level in (logging.NOTSET, logging.DEBUG, logging.INFO):
            mod_logger.setLevel(logging.WARNING)


def _init_with_fallback(factory: Any, candidates: List[Dict[str, Any]]):
    last_exc: Optional[Exception] = None
    for kwargs in candidates:
        try:
            return factory(**kwargs)
        except (TypeError, ValueError) as exc:
            last_exc = exc
            logger.debug(
                "Constructor %s(%s) failed: %s",
                getattr(factory, "__name__", factory),
                kwargs,
                exc,
            )
    assert last_exc is not None
    raise last_exc


def get_paddle_ocr(lang: str = "vie"):
    global _paddle_ocr, _paddle_ocr_lang
    mapped = map_lang_for_paddle(lang)
    if _paddle_ocr is not None and _paddle_ocr_lang == mapped:
        return _paddle_ocr
    import paddleocr
    from paddleocr import PaddleOCR

    if _is_paddleocr_v3(paddleocr, PaddleOCR):
        _silence_paddle_logger(paddleocr)
        candidates = [
            {
                "lang": mapped,
                "use_doc_orientation_classify": False,
                "use_doc_unwarping": False,
                "use_textline_orientation": False,
            },
            {"lang": mapped},
        ]
    else:
        # PaddleOCR 2.x fallback (supports show_log=False and use_angle_cls)
        candidates = [
            {"use_angle_cls": True, "lang": mapped, "show_log": False, "use_gpu": False},
            {"use_angle_cls": True, "lang": mapped, "show_log": False},
            {"use_angle_cls": True, "lang": mapped},
            {"lang": mapped},
        ]

    _paddle_ocr = _init_with_fallback(PaddleOCR, candidates)
    _paddle_ocr_lang = mapped
    logger.info("Loaded PaddleOCR (lang=%s)", mapped)
    return _paddle_ocr


def get_structure_engine(lang: str = "vie"):
    """Prefer PPStructureV3; fall back to classic PPStructure."""
    global _structure_engine, _structure_lang
    mapped = map_lang_for_paddle(lang)
    if _structure_engine is not None and _structure_lang == mapped:
        return _structure_engine

    # Try modern pipeline first (PaddleOCR 3.x; no show_log parameter)
    try:
        import paddleocr
        from paddleocr import PPStructureV3

        _silence_paddle_logger(paddleocr)
        _structure_engine = _init_with_fallback(PPStructureV3, [{"lang": mapped}, {}])
        _structure_lang = mapped
        logger.info("Loaded PPStructureV3 (lang=%s)", mapped)
        return _structure_engine
    except Exception as exc:
        logger.debug("PPStructureV3 unavailable: %s", exc)

    try:
        import paddleocr
        from paddleocr import PPStructure

        if _is_paddleocr_v3(paddleocr, PPStructure):
            _silence_paddle_logger(paddleocr)
            structure_candidates = [{"lang": mapped}, {}]
        else:
            structure_candidates = [
                {"show_log": False, "lang": mapped},
                {"lang": mapped},
                {"show_log": False},
                {},
            ]
        _structure_engine = _init_with_fallback(PPStructure, structure_candidates)
        _structure_lang = mapped
        logger.info("Loaded classic PPStructure")
        return _structure_engine
    except Exception as exc:
        raise RuntimeError(
            "PP-Structure requires a recent paddleocr with structure support. "
            "Install/update with: pip install -U paddleocr paddlepaddle"
        ) from exc


def _bbox_rect(box) -> Optional[Tuple[float, float, float, float]]:
    """Return (x0, y0, x1, y1) from polygon or [x0, y0, x1, y1] bounding box."""
    try:
        if isinstance(box, (list, tuple)) and len(box) >= 2:
            if isinstance(box[0], (list, tuple)):
                xs = [float(p[0]) for p in box]
                ys = [float(p[1]) for p in box]
                return (min(xs), min(ys), max(xs), max(ys))
            if len(box) >= 4:
                return (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
    except Exception:
        pass
    return None


def _split_x_columns(
    items: List[Tuple[str, float, Tuple[float, float, float, float]]],
    width_hint: float,
) -> List[List[Tuple[str, float, Tuple[float, float, float, float]]]]:
    """Split a group of narrow boxes into columns using X-interval gutters."""
    if len(items) < 4 or width_hint <= 0:
        return [items]
    min_gutter = max(30.0, width_hint * 0.04)
    sorted_by_x = sorted(items, key=lambda item: (item[2][0], item[2][2]))
    groups: List[List[Tuple[str, float, Tuple[float, float, float, float]]]] = []
    cur_group: List[Tuple[str, float, Tuple[float, float, float, float]]] = []
    cur_right = -1.0

    for item in sorted_by_x:
        x0, _, x1, _ = item[2]
        if cur_group and (x0 - cur_right) >= min_gutter:
            groups.append(cur_group)
            cur_group = [item]
            cur_right = x1
        else:
            cur_group.append(item)
            cur_right = max(cur_right, x1)
    if cur_group:
        groups.append(cur_group)

    # Require at least 2 boxes per column so isolated right-aligned words stay inline.
    if len(groups) <= 1 or any(len(g) < 2 for g in groups):
        return [items]
    return groups


def _cluster_ocr_columns(
    items: List[Tuple[str, float, Tuple[float, float, float, float]]],
) -> List[List[Tuple[str, float, Tuple[float, float, float, float]]]]:
    """Order full-width spans in Y order and split multi-column regions between them."""
    if not items:
        return []
    width_hint = max(rect[2] for _, _, rect in items)
    if width_hint <= 0:
        return [items]

    wide_threshold = width_hint * 0.55
    ordered_by_y = sorted(items, key=lambda item: ((item[2][1] + item[2][3]) / 2.0, item[2][0]))
    segments: List[List[Tuple[str, float, Tuple[float, float, float, float]]]] = []
    narrow_band: List[Tuple[str, float, Tuple[float, float, float, float]]] = []

    for item in ordered_by_y:
        box_width = item[2][2] - item[2][0]
        if box_width >= wide_threshold:
            if narrow_band:
                segments.extend(_split_x_columns(narrow_band, width_hint))
                narrow_band = []
            segments.append([item])
        else:
            narrow_band.append(item)
    if narrow_band:
        segments.extend(_split_x_columns(narrow_band, width_hint))
    return segments


def run_plain_ocr_on_array(img_array, lang: str = "vie") -> List[Block]:
    """Plain PaddleOCR → ordered paragraph blocks (line-grouped & multi-column aware)."""
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

    normalized: List[Tuple[str, float, Tuple[float, float, float, float]]] = []
    for text, score, box in lines_data:
        cleaned = (text or "").strip()
        if not cleaned:
            continue
        rect = _bbox_rect(box) or (0.0, 0.0, 0.0, 0.0)
        try:
            conf = float(score)
        except Exception:
            conf = 0.85
        normalized.append((cleaned, conf, rect))

    if not normalized:
        return []

    heights = [rect[3] - rect[1] for _, _, rect in normalized if rect[3] > rect[1]]
    median_height = median(heights) if heights else 20.0
    same_line_tol = max(6.0, median_height * 0.55)
    para_gap_tol = max(12.0, median_height * 1.4)

    blocks: List[Block] = []
    for col_items in _cluster_ocr_columns(normalized):
        col_items.sort(key=lambda item: ((item[2][1] + item[2][3]) / 2.0, item[2][0]))
        visual_lines: List[List[Tuple[str, float, Tuple[float, float, float, float]]]] = []
        for item in col_items:
            cy = (item[2][1] + item[2][3]) / 2.0
            if visual_lines:
                prev_cy = sum((x[2][1] + x[2][3]) / 2.0 for x in visual_lines[-1]) / len(visual_lines[-1])
                if abs(cy - prev_cy) <= same_line_tol:
                    visual_lines[-1].append(item)
                    continue
            visual_lines.append([item])

        para_lines: List[str] = []
        para_scores: List[float] = []
        para_rects: List[Tuple[float, float, float, float]] = []
        prev_bottom: Optional[float] = None

        for line in visual_lines:
            line.sort(key=lambda item: item[2][0])
            line_top = min(item[2][1] for item in line)
            line_bottom = max(item[2][3] for item in line)
            line_text = " ".join(item[0] for item in line)

            if prev_bottom is not None and (line_top - prev_bottom) > para_gap_tol and para_lines:
                bbox = [
                    round(min(r[0] for r in para_rects), 1),
                    round(min(r[1] for r in para_rects), 1),
                    round(max(r[2] for r in para_rects), 1),
                    round(max(r[3] for r in para_rects), 1),
                ]
                blocks.append(
                    Block(
                        "paragraph",
                        " ".join(para_lines),
                        source={"bbox": bbox} if any(bbox) else {},
                        confidence=sum(para_scores) / len(para_scores) if para_scores else None,
                        metadata={"ocr_engine": "paddle"},
                    )
                )
                para_lines, para_scores, para_rects = [], [], []

            para_lines.append(line_text)
            para_scores.extend(item[1] for item in line)
            para_rects.extend(item[2] for item in line)
            prev_bottom = line_bottom

        if para_lines:
            bbox = [
                round(min(r[0] for r in para_rects), 1),
                round(min(r[1] for r in para_rects), 1),
                round(max(r[2] for r in para_rects), 1),
                round(max(r[3] for r in para_rects), 1),
            ]
            blocks.append(
                Block(
                    "paragraph",
                    " ".join(para_lines),
                    source={"bbox": bbox} if any(bbox) else {},
                    confidence=sum(para_scores) / len(para_scores) if para_scores else None,
                    metadata={"ocr_engine": "paddle"},
                )
            )
    return blocks


def ocr_pil_image_tesseract(
    image,
    lang: str = "vie",
    page_number: Optional[int] = None,
) -> List[Block]:
    """Run Tesseract OCR on a PIL image and return ordered paragraph blocks with confidence."""
    try:
        import pytesseract
    except ImportError as exc:
        raise RuntimeError("Tesseract OCR requires pytesseract and Pillow") from exc

    tess_lang = map_lang_for_tesseract(lang)
    data = pytesseract.image_to_data(image, lang=tess_lang, output_type=pytesseract.Output.DICT)
    total = len(data.get("text", []))
    groups: Dict[Tuple[int, int], List[int]] = {}
    for index in range(total):
        if str(data.get("text", [""] * total)[index] or "").strip():
            key = (
                int(data.get("block_num", [0] * total)[index] or 0),
                int(data.get("line_num", [0] * total)[index] or 0),
            )
            groups.setdefault(key, []).append(index)

    source_base = {"page": page_number} if page_number is not None else {}
    blocks: List[Block] = []
    for (block_number, _), indices in sorted(
        groups.items(),
        key=lambda item: (
            item[0][0],
            min(int(data.get("top", [0] * total)[i] or 0) for i in item[1]),
        ),
    ):
        indices.sort(key=lambda i: int(data.get("left", [0] * total)[i] or 0))
        text = " ".join(str(data["text"][i]) for i in indices).strip()
        if text:
            confidence_values = [
                float(data.get("conf", [0] * total)[i])
                for i in indices
                if float(data.get("conf", [0] * total)[i]) >= 0
            ]
            confidence = (
                (sum(confidence_values) / len(confidence_values) / 100.0) if confidence_values else None
            )
            blocks.append(
                Block(
                    "paragraph",
                    text,
                    source=dict(source_base),
                    confidence=confidence,
                    metadata={"ocr_engine": "tesseract", "ocr_block": block_number},
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
        if isinstance(md, dict):
            md = md.get("markdown_texts") or md.get("text") or ""
        if md and isinstance(md, str) and md.strip():
            # Keep as pre-formatted Markdown block; renderer will pass it through unescaped
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

        # Title / heading (do not promote running page 'header' to heading)
        if any(k in typ for k in ("title", "heading", "doc_title", "paragraph_title", "section_header")):
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
    return any(
        re.match(r"^\s*(?:[•●▪◦·-]|\d+[.)]|[A-Za-z][.)])\s+", line)
        for line in text.splitlines()
    )


def run_structure_on_image(img_array_or_path, lang: str = "vie", page_number: Optional[int] = None) -> List[Block]:
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
