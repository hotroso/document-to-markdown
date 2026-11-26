"""Shared PaddleOCR / PP-Structure helpers and language mapping."""

from __future__ import annotations

import importlib.util
import inspect
import io
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

from .models import Block

logger = logging.getLogger(__name__)

_paddle_ocr = None
_paddle_ocr_key: Optional[Tuple[Any, ...]] = None
_paddle_ocr_mkldnn_disabled: bool = False
_structure_engine = None
_structure_key: Optional[Tuple[Any, ...]] = None
_structure_mkldnn_disabled: bool = False
_force_disable_mkldnn: bool = False

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


def _env_flag_bool(name: str) -> Optional[bool]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    val = raw.strip().lower()
    if val in {"1", "true", "yes", "on"}:
        return True
    if val in {"0", "false", "no", "off"}:
        return False
    return None


def _should_disable_mkldnn() -> bool:
    """Disable oneDNN/MKLDNN by default on Windows (avoids Paddle 3.3 PIR bug) or when requested."""
    if _force_disable_mkldnn:
        return True
    env_pdx = _env_flag_bool("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT")
    if env_pdx is not None:
        return not env_pdx
    env_flag = _env_flag_bool("FLAGS_use_mkldnn")
    if env_flag is not None:
        return not env_flag
    return sys.platform == "win32"


def _prepare_paddle_env(force_disable_mkldnn: bool = False) -> None:
    """Configure PaddleX/Paddle env vars before importing paddleocr."""
    if force_disable_mkldnn:
        os.environ["PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT"] = "0"
        os.environ["FLAGS_use_mkldnn"] = "0"
    elif _should_disable_mkldnn():
        os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "0")
        os.environ.setdefault("FLAGS_use_mkldnn", "0")


def _is_pir_mkldnn_error(exc: BaseException) -> bool:
    """Detect PaddlePaddle 3.x PIR -> oneDNN/MKLDNN runtime incompatibility errors."""
    msg = str(exc)
    return "ConvertPirAttribute2RuntimeAttribute" in msg or (
        "onednn" in msg.lower() and "pir" in msg.lower()
    )


def _module_available(name: str) -> bool:
    """Check whether a module is installed without importing it (avoids heavy Paddle startup)."""
    if name in sys.modules:
        return sys.modules[name] is not None
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def detect_ocr_engine(requested: str) -> str:
    """Resolve requested OCR engine ('auto' prefers PaddleOCR, falls back to Tesseract)."""
    requested = (requested or "auto").lower()
    if requested in {"off", "none"}:
        return "off"
    if requested == "tesseract":
        return "tesseract"
    if requested == "paddle":
        return "paddle"
    if _module_available("paddleocr"):
        return "paddle"
    if _module_available("pytesseract"):
        return "tesseract"
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


def _normalize_model_options(
    ocr_version: Optional[str] = None,
    ocr_profile: str = "auto",
    det_model: Optional[str] = None,
    rec_model: Optional[str] = None,
) -> Tuple[Optional[str], str, Optional[str], Optional[str]]:
    version = (ocr_version or "").strip() or None
    profile = (ocr_profile or "auto").strip().lower()
    if profile not in {"auto", "mobile", "server"}:
        profile = "auto"
    det = (det_model or "").strip() or None
    rec = (rec_model or "").strip() or None
    return version, profile, det, rec


def _resolve_paddle_model_kwargs(
    mapped_lang: str,
    ocr_version: Optional[str] = None,
    ocr_profile: str = "auto",
    det_model: Optional[str] = None,
    rec_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Build optional PaddleOCR 3.x model kwargs without altering defaults when unset."""
    version, profile, det, rec = _normalize_model_options(
        ocr_version=ocr_version,
        ocr_profile=ocr_profile,
        det_model=det_model,
        rec_model=rec_model,
    )
    if profile in {"mobile", "server"}:
        ver_prefix = version or "PP-OCRv5"
        if det is None:
            det = f"{ver_prefix}_{profile}_det"
        # For non-Chinese scripts (e.g. vi -> latin_PP-OCRv5_mobile_rec), let Paddle pick
        # the matching script recognition model unless explicitly overridden via rec_model.
        if rec is None and mapped_lang == "ch":
            rec = f"{ver_prefix}_{profile}_rec"

    kwargs: Dict[str, Any] = {}
    if version is not None:
        kwargs["ocr_version"] = version
    if det is not None:
        kwargs["text_detection_model_name"] = det
    if rec is not None:
        kwargs["text_recognition_model_name"] = rec
    return kwargs


def _dedupe_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    unique: List[Dict[str, Any]] = []
    for item in candidates:
        key = tuple(sorted(item.items()))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _init_with_fallback(factory: Any, candidates: List[Dict[str, Any]]):
    last_exc: Optional[Exception] = None
    for kwargs in _dedupe_candidates(candidates):
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


def get_paddle_ocr(
    lang: str = "vie",
    disable_mkldnn: Optional[bool] = None,
    ocr_version: Optional[str] = None,
    ocr_profile: str = "auto",
    det_model: Optional[str] = None,
    rec_model: Optional[str] = None,
):
    global _paddle_ocr, _paddle_ocr_key, _paddle_ocr_mkldnn_disabled, _force_disable_mkldnn
    mapped = map_lang_for_paddle(lang)
    version, profile, det, rec = _normalize_model_options(
        ocr_version=ocr_version,
        ocr_profile=ocr_profile,
        det_model=det_model,
        rec_model=rec_model,
    )
    cache_key = (mapped, version, profile, det, rec)
    effective_disable_mkldnn = (
        _should_disable_mkldnn() if disable_mkldnn is None else bool(disable_mkldnn)
    )
    if (
        _paddle_ocr is not None
        and _paddle_ocr_key == cache_key
        and (not effective_disable_mkldnn or _paddle_ocr_mkldnn_disabled)
    ):
        return _paddle_ocr

    _prepare_paddle_env(force_disable_mkldnn=bool(disable_mkldnn))
    import paddleocr
    from paddleocr import PaddleOCR

    is_v3 = _is_paddleocr_v3(paddleocr, PaddleOCR)
    model_kwargs = _resolve_paddle_model_kwargs(
        mapped,
        ocr_version=version,
        ocr_profile=profile,
        det_model=det,
        rec_model=rec,
    )

    if is_v3:
        _silence_paddle_logger(paddleocr)
        base_v3 = {
            "lang": mapped,
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
        }
        mkldnn_extra = {"enable_mkldnn": False} if effective_disable_mkldnn else {}
        candidates: List[Dict[str, Any]] = []
        if model_kwargs:
            candidates.append({**base_v3, **model_kwargs, **mkldnn_extra})
            candidates.append({"lang": mapped, **model_kwargs, **mkldnn_extra})
            if "ocr_version" in model_kwargs and len(model_kwargs) > 1:
                no_ver = {k: v for k, v in model_kwargs.items() if k != "ocr_version"}
                candidates.append({**base_v3, **no_ver, **mkldnn_extra})
                candidates.append({"lang": mapped, **no_ver, **mkldnn_extra})
            if mkldnn_extra:
                candidates.append({**base_v3, **model_kwargs})
                candidates.append({"lang": mapped, **model_kwargs})
        if mkldnn_extra:
            candidates.append({**base_v3, **mkldnn_extra})
            candidates.append({"lang": mapped, **mkldnn_extra})
        candidates.extend([base_v3, {"lang": mapped}])
    else:
        # PaddleOCR 2.x fallback (supports show_log=False, use_angle_cls, and ocr_version)
        base_v2 = {"use_angle_cls": True, "lang": mapped, "show_log": False, "use_gpu": False}
        ver_extra = {"ocr_version": version} if version else {}
        mkldnn_extra = {"enable_mkldnn": False} if effective_disable_mkldnn else {}
        candidates = []
        if ver_extra:
            if mkldnn_extra:
                candidates.append({**base_v2, **ver_extra, **mkldnn_extra})
            candidates.append({**base_v2, **ver_extra})
            candidates.append({"use_angle_cls": True, "lang": mapped, "show_log": False, **ver_extra})
            candidates.append({"use_angle_cls": True, "lang": mapped, **ver_extra})
        if mkldnn_extra:
            candidates.append({**base_v2, **mkldnn_extra})
        candidates.extend(
            [
                base_v2,
                {"use_angle_cls": True, "lang": mapped, "show_log": False},
                {"use_angle_cls": True, "lang": mapped},
                {"lang": mapped},
            ]
        )

    try:
        _paddle_ocr = _init_with_fallback(PaddleOCR, candidates)
    except Exception as exc:
        if not effective_disable_mkldnn and _is_pir_mkldnn_error(exc):
            logger.warning(
                "PaddleOCR init hit PIR/oneDNN error (%s); retrying with enable_mkldnn=False.",
                exc,
            )
            _force_disable_mkldnn = True
            return get_paddle_ocr(
                lang,
                disable_mkldnn=True,
                ocr_version=version,
                ocr_profile=profile,
                det_model=det,
                rec_model=rec,
            )
        raise

    _paddle_ocr_key = cache_key
    _paddle_ocr_mkldnn_disabled = effective_disable_mkldnn
    logger.info(
        "Loaded PaddleOCR (lang=%s, profile=%s, version=%s, mkldnn=%s)",
        mapped,
        profile,
        version or "default",
        "off" if effective_disable_mkldnn else "default",
    )
    return _paddle_ocr


def get_structure_engine(
    lang: str = "vie",
    disable_mkldnn: Optional[bool] = None,
    ocr_version: Optional[str] = None,
    ocr_profile: str = "auto",
    det_model: Optional[str] = None,
    rec_model: Optional[str] = None,
):
    """Prefer PPStructureV3; fall back to classic PPStructure."""
    global _structure_engine, _structure_key, _structure_mkldnn_disabled, _force_disable_mkldnn
    mapped = map_lang_for_paddle(lang)
    version, profile, det, rec = _normalize_model_options(
        ocr_version=ocr_version,
        ocr_profile=ocr_profile,
        det_model=det_model,
        rec_model=rec_model,
    )
    cache_key = (mapped, version, profile, det, rec)
    effective_disable_mkldnn = (
        _should_disable_mkldnn() if disable_mkldnn is None else bool(disable_mkldnn)
    )
    if (
        _structure_engine is not None
        and _structure_key == cache_key
        and (not effective_disable_mkldnn or _structure_mkldnn_disabled)
    ):
        return _structure_engine

    _prepare_paddle_env(force_disable_mkldnn=bool(disable_mkldnn))
    model_kwargs = _resolve_paddle_model_kwargs(
        mapped,
        ocr_version=version,
        ocr_profile=profile,
        det_model=det,
        rec_model=rec,
    )
    mkldnn_extra = {"enable_mkldnn": False} if effective_disable_mkldnn else {}

    # Try modern pipeline first (PaddleOCR 3.x; no show_log parameter)
    try:
        import paddleocr
        from paddleocr import PPStructureV3

        _silence_paddle_logger(paddleocr)
        v3_candidates: List[Dict[str, Any]] = []
        if model_kwargs:
            v3_candidates.append({"lang": mapped, **model_kwargs, **mkldnn_extra})
            if "ocr_version" in model_kwargs and len(model_kwargs) > 1:
                no_ver = {k: v for k, v in model_kwargs.items() if k != "ocr_version"}
                v3_candidates.append({"lang": mapped, **no_ver, **mkldnn_extra})
                v3_candidates.append({**no_ver, **mkldnn_extra})
            v3_candidates.append({**model_kwargs, **mkldnn_extra})
            if mkldnn_extra:
                v3_candidates.append({"lang": mapped, **model_kwargs})
                v3_candidates.append({**model_kwargs})
        if mkldnn_extra:
            v3_candidates.extend([{"lang": mapped, **mkldnn_extra}, dict(mkldnn_extra)])
        v3_candidates.extend([{"lang": mapped}, {}])

        _structure_engine = _init_with_fallback(PPStructureV3, v3_candidates)
        _structure_key = cache_key
        _structure_mkldnn_disabled = effective_disable_mkldnn
        logger.info(
            "Loaded PPStructureV3 (lang=%s, profile=%s, version=%s, mkldnn=%s)",
            mapped,
            profile,
            version or "default",
            "off" if effective_disable_mkldnn else "default",
        )
        return _structure_engine
    except Exception as exc:
        if not effective_disable_mkldnn and _is_pir_mkldnn_error(exc):
            logger.warning(
                "PPStructureV3 init hit PIR/oneDNN error (%s); retrying with enable_mkldnn=False.",
                exc,
            )
            _force_disable_mkldnn = True
            return get_structure_engine(
                lang,
                disable_mkldnn=True,
                ocr_version=version,
                ocr_profile=profile,
                det_model=det,
                rec_model=rec,
            )
        logger.debug("PPStructureV3 unavailable: %s", exc)

    try:
        import paddleocr
        from paddleocr import PPStructure

        if _is_paddleocr_v3(paddleocr, PPStructure):
            _silence_paddle_logger(paddleocr)
            structure_candidates = (
                [{"lang": mapped, "enable_mkldnn": False}, {"enable_mkldnn": False}]
                if effective_disable_mkldnn
                else []
            ) + [{"lang": mapped}, {}]
        else:
            structure_candidates = (
                [{"show_log": False, "lang": mapped, "enable_mkldnn": False}]
                if effective_disable_mkldnn
                else []
            ) + [
                {"show_log": False, "lang": mapped},
                {"lang": mapped},
                {"show_log": False},
                {},
            ]
        _structure_engine = _init_with_fallback(PPStructure, structure_candidates)
        _structure_key = cache_key
        _structure_mkldnn_disabled = effective_disable_mkldnn
        logger.info("Loaded classic PPStructure")
        return _structure_engine
    except Exception as exc:
        raise RuntimeError(
            "PP-Structure requires a recent paddleocr with structure support. "
            "Install/update with: pip install -U paddleocr paddlepaddle"
        ) from exc


def _to_list(value: Any) -> Optional[List[Any]]:
    """Safely convert a sequence or NumPy-like array to a Python list without truthiness checks."""
    if value is None:
        return None
    if hasattr(value, "tolist"):
        converted = value.tolist()
        return converted if isinstance(converted, list) else [converted]
    if isinstance(value, (list, tuple)):
        return list(value)
    try:
        return list(value)
    except TypeError:
        return None


def _get_mapping_or_attr(obj: Any, key: str) -> Any:
    if isinstance(obj, dict) and key in obj:
        return obj[key]
    if hasattr(obj, key):
        return getattr(obj, key)
    if hasattr(obj, "get"):
        try:
            return obj.get(key)
        except Exception:
            pass
    return None


def _first_non_empty_list(obj: Any, *keys: str) -> List[Any]:
    for key in keys:
        val = _to_list(_get_mapping_or_attr(obj, key))
        if val is not None and len(val) > 0:
            return val
    return []


def _first_present(obj: Any, *keys: str) -> Any:
    """Return the first non-None, non-empty field without calling bool() on NumPy arrays."""
    for key in keys:
        val = _get_mapping_or_attr(obj, key)
        if val is None:
            continue
        if hasattr(val, "size"):
            if int(val.size) > 0:
                return val
        elif isinstance(val, (list, tuple, dict, str)):
            if len(val) > 0:
                return val
        else:
            return val
    return None


def _bbox_rect(box) -> Optional[Tuple[float, float, float, float]]:
    """Return (x0, y0, x1, y1) from polygon or [x0, y0, x1, y1] bounding box."""
    try:
        if hasattr(box, "tolist"):
            box = box.tolist()
        if isinstance(box, (list, tuple)) and len(box) >= 2:
            first = box[0].tolist() if hasattr(box[0], "tolist") else box[0]
            if isinstance(first, (list, tuple)):
                pts = [p.tolist() if hasattr(p, "tolist") else p for p in box]
                xs = [float(p[0]) for p in pts]
                ys = [float(p[1]) for p in pts]
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


def run_plain_ocr_on_array(
    img_array,
    lang: str = "vie",
    ocr_version: Optional[str] = None,
    ocr_profile: str = "auto",
    det_model: Optional[str] = None,
    rec_model: Optional[str] = None,
) -> List[Block]:
    """Plain PaddleOCR → ordered paragraph blocks (line-grouped & multi-column aware)."""
    global _force_disable_mkldnn
    model_opts: Dict[str, Any] = {}
    if ocr_version is not None:
        model_opts["ocr_version"] = ocr_version
    if (ocr_profile or "auto").strip().lower() != "auto":
        model_opts["ocr_profile"] = ocr_profile
    if det_model is not None:
        model_opts["det_model"] = det_model
    if rec_model is not None:
        model_opts["rec_model"] = rec_model

    engine = get_paddle_ocr(lang, **model_opts)
    lines_data: List[Tuple[str, float, Any]] = []

    if hasattr(engine, "predict"):
        try:
            preds = engine.predict(img_array)
        except Exception as exc:
            if not _is_pir_mkldnn_error(exc):
                raise
            logger.warning(
                "PaddleOCR predict hit PIR/oneDNN error (%s); retrying with enable_mkldnn=False.",
                exc,
            )
            _force_disable_mkldnn = True
            engine = get_paddle_ocr(lang, disable_mkldnn=True, **model_opts)
            preds = engine.predict(img_array)
        preds_list = list(preds) if preds is not None else []
        if preds_list:
            res = preds_list[0]
            texts = _first_non_empty_list(res, "rec_texts")
            scores = _first_non_empty_list(res, "rec_scores")
            boxes = _first_non_empty_list(res, "rec_boxes", "dt_polys", "rec_polys")
            if len(scores) < len(texts):
                scores = scores + [0.9] * (len(texts) - len(scores))
            if len(boxes) < len(texts):
                boxes = boxes + [None] * (len(texts) - len(boxes))
            lines_data = list(zip(texts, scores, boxes))
    else:
        try:
            ocr_out = engine.ocr(img_array, cls=True)
        except Exception as exc:
            if not _is_pir_mkldnn_error(exc):
                raise
            logger.warning(
                "PaddleOCR ocr hit PIR/oneDNN error (%s); retrying with enable_mkldnn=False.",
                exc,
            )
            _force_disable_mkldnn = True
            engine = get_paddle_ocr(lang, disable_mkldnn=True, **model_opts)
            ocr_out = engine.ocr(img_array, cls=True)
        if ocr_out is not None and len(ocr_out) > 0 and ocr_out[0] is not None:
            for item in ocr_out[0]:
                if item is None:
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
    md = _get_mapping_or_attr(result, "markdown")
    if md is not None:
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
        extracted = _first_present(result, "res", "layout", "boxes")
        items = extracted if extracted is not None else [result]
    if hasattr(items, "tolist"):
        items = items.tolist()
    if not isinstance(items, list):
        items = [items]

    # Sort by reading order: top → bottom, left → right (multi-column friendly)
    def item_key(it):
        bbox = _first_present(it, "bbox", "coordinate", "box")
        rect = _bbox_rect(bbox)
        if rect is None:
            return (0.0, 0.0)
        return (rect[1], rect[0])

    try:
        items = sorted(items, key=item_key)
    except Exception:
        pass

    for it in items:
        if not isinstance(it, dict):
            continue
        typ = (it.get("type") or it.get("label") or "text").lower()
        bbox = _first_present(it, "bbox", "coordinate", "box")
        res = it.get("res")
        conf = it.get("score")
        if conf is None:
            conf = it.get("confidence")
        source = dict(source_base)
        rect = _bbox_rect(bbox)
        if rect is not None:
            source["bbox"] = [round(v, 1) for v in rect]

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


def run_structure_on_image(
    img_array_or_path,
    lang: str = "vie",
    page_number: Optional[int] = None,
    ocr_version: Optional[str] = None,
    ocr_profile: str = "auto",
    det_model: Optional[str] = None,
    rec_model: Optional[str] = None,
) -> List[Block]:
    """Full layout + table + reading-order analysis."""
    global _force_disable_mkldnn
    model_opts: Dict[str, Any] = {}
    if ocr_version is not None:
        model_opts["ocr_version"] = ocr_version
    if (ocr_profile or "auto").strip().lower() != "auto":
        model_opts["ocr_profile"] = ocr_profile
    if det_model is not None:
        model_opts["det_model"] = det_model
    if rec_model is not None:
        model_opts["rec_model"] = rec_model

    engine = get_structure_engine(lang, **model_opts)

    # PPStructureV3
    if hasattr(engine, "predict"):
        try:
            outputs = engine.predict(img_array_or_path)
        except Exception as exc:
            if not _is_pir_mkldnn_error(exc):
                raise
            logger.warning(
                "PP-Structure predict hit PIR/oneDNN error (%s); retrying with enable_mkldnn=False.",
                exc,
            )
            _force_disable_mkldnn = True
            engine = get_structure_engine(lang, disable_mkldnn=True, **model_opts)
            outputs = engine.predict(img_array_or_path)
        blocks: List[Block] = []
        for res in outputs or []:
            blocks.extend(structure_result_to_blocks(res, page_number=page_number))
        if blocks:
            return blocks

    # Classic PPStructure callable
    if callable(engine):
        try:
            result = engine(img_array_or_path)
        except Exception as exc:
            if not _is_pir_mkldnn_error(exc):
                raise
            logger.warning(
                "PP-Structure call hit PIR/oneDNN error (%s); retrying with enable_mkldnn=False.",
                exc,
            )
            _force_disable_mkldnn = True
            engine = get_structure_engine(lang, disable_mkldnn=True, **model_opts)
            result = engine(img_array_or_path)
        return structure_result_to_blocks(result, page_number=page_number)

    return []
