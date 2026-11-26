"""Direct image → Markdown adapter using PP-Structure / PaddleOCR."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from .models import Block, Document
from .ocr_engine import (
    detect_ocr_engine,
    ocr_pil_image_tesseract,
    run_plain_ocr_on_array,
    run_structure_on_image,
)

logger = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def _ocr_single_frame(
    frame,
    img_array,
    ocr: str,
    engine: str,
    ocr_lang: str,
    use_structure: bool,
    page_number: Optional[int],
    document: Document,
    ocr_version: Optional[str] = None,
    ocr_profile: str = "auto",
    det_model: Optional[str] = None,
    rec_model: Optional[str] = None,
) -> List[Block]:
    blocks: List[Block] = []
    model_kw = {
        "ocr_version": ocr_version,
        "ocr_profile": ocr_profile,
        "det_model": det_model,
        "rec_model": rec_model,
    }
    if engine == "paddle":
        if use_structure:
            try:
                blocks = run_structure_on_image(
                    img_array, lang=ocr_lang, page_number=page_number, **model_kw
                )
                if blocks:
                    document.metadata["ocr_engine"] = "ppstructure"
                    return blocks
                document.warn("Structure analysis returned no blocks; falling back to plain OCR.")
            except Exception as exc:
                document.warn(f"Structure analysis failed ({exc}); falling back to plain OCR.")

        try:
            blocks = run_plain_ocr_on_array(img_array, lang=ocr_lang, **model_kw)
            if page_number is not None:
                for b in blocks:
                    b.source = {**b.source, "page": page_number}
            document.metadata["ocr_engine"] = "paddle"
            return blocks
        except Exception as exc:
            if (ocr or "auto").lower() == "auto":
                try:
                    blocks = ocr_pil_image_tesseract(frame, lang=ocr_lang, page_number=page_number)
                    document.warn(f"PaddleOCR failed ({exc}); fell back to Tesseract.")
                    document.metadata["ocr_engine"] = "tesseract"
                    return blocks
                except Exception:
                    pass
            document.warn(f"PaddleOCR failed ({exc})")
            raise RuntimeError(f"OCR failed for image: {exc}") from exc

    if engine == "tesseract":
        try:
            blocks = ocr_pil_image_tesseract(frame, lang=ocr_lang, page_number=page_number)
            document.metadata["ocr_engine"] = "tesseract"
            return blocks
        except Exception as exc:
            raise RuntimeError(f"Tesseract OCR failed: {exc}") from exc

    raise RuntimeError("OCR is required for image inputs; use --ocr auto|paddle|tesseract")


def convert_image(
    path: Path,
    ocr: str = "auto",
    ocr_lang: str = "vie",
    use_structure: bool = True,
    ocr_version: Optional[str] = None,
    ocr_profile: str = "auto",
    det_model: Optional[str] = None,
    rec_model: Optional[str] = None,
) -> Document:
    """Convert an image file (including multi-page TIFF) to a Document using layout-aware OCR."""
    try:
        from PIL import Image, ImageOps, ImageSequence
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("Image support requires Pillow and numpy (install with [ocr] extra)") from exc

    engine = detect_ocr_engine(ocr)
    if engine == "off":
        raise RuntimeError(
            "No OCR engine available (install paddleocr or pytesseract, or check --ocr)."
        )

    meta = {"ocr_mode": ocr, "ocr_engine": engine, "use_structure": use_structure}
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
        "image",
        path.stem,
        metadata=meta,
    )

    with Image.open(path) as pil_file:
        if getattr(pil_file, "n_frames", 1) <= 1:
            frames = [ImageOps.exif_transpose(pil_file).convert("RGB")]
        else:
            frames = [
                ImageOps.exif_transpose(frame).convert("RGB")
                for frame in ImageSequence.Iterator(pil_file)
            ]

    multi_frame = len(frames) > 1
    for idx, frame in enumerate(frames):
        page_number = idx + 1 if multi_frame else None
        img_array = np.array(frame)
        frame_blocks = _ocr_single_frame(
            frame,
            img_array,
            ocr=ocr,
            engine=engine,
            ocr_lang=ocr_lang,
            use_structure=use_structure,
            page_number=page_number,
            document=document,
            ocr_version=ocr_version,
            ocr_profile=ocr_profile,
            det_model=det_model,
            rec_model=rec_model,
        )
        for b in frame_blocks:
            document.add(b)
        if multi_frame and idx < len(frames) - 1:
            document.add(Block("page_break", source={"page": idx + 1}))

    if not document.blocks:
        document.warn("No text detected in image.")
        document.add(Block("paragraph", "(no text detected)"))
    return document
