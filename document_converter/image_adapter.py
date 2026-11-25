"""Direct image → Markdown adapter using PP-Structure / PaddleOCR."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .models import Block, Document
from .ocr_engine import run_plain_ocr_on_array, run_structure_on_image

logger = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def convert_image(
    path: Path,
    ocr: str = "auto",
    ocr_lang: str = "eng",
    use_structure: bool = True,
) -> Document:
    """Convert a single image file to a Document using layout-aware OCR."""
    try:
        from PIL import Image
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("Image support requires Pillow and numpy (install with [ocr] extra)") from exc

    document = Document(
        str(path),
        "image",
        path.stem,
        metadata={"ocr_mode": ocr, "use_structure": use_structure},
    )

    image = Image.open(path).convert("RGB")
    img_array = np.array(image)

    blocks: list[Block] = []
    engine = (ocr or "auto").lower()

    if engine in {"auto", "paddle"} and use_structure:
        try:
            blocks = run_structure_on_image(img_array, lang=ocr_lang)
            if blocks:
                document.metadata["ocr_engine"] = "ppstructure"
                for b in blocks:
                    document.add(b)
                return document
            document.warn("Structure analysis returned no blocks; falling back to plain OCR.")
        except Exception as exc:
            document.warn(f"Structure analysis failed ({exc}); falling back to plain OCR.")

    if engine in {"auto", "paddle"}:
        try:
            blocks = run_plain_ocr_on_array(img_array, lang=ocr_lang)
            document.metadata["ocr_engine"] = "paddle"
        except Exception as exc:
            document.warn(f"PaddleOCR failed ({exc})")
            raise RuntimeError(f"OCR failed for image {path.name}: {exc}") from exc
    elif engine == "tesseract":
        try:
            import pytesseract
            from PIL import Image as PILImage

            data = pytesseract.image_to_string(PILImage.open(path), lang=ocr_lang)
            for para in data.split("\n\n"):
                para = para.strip()
                if para:
                    document.add(Block("paragraph", para, metadata={"ocr_engine": "tesseract"}))
            document.metadata["ocr_engine"] = "tesseract"
            return document
        except Exception as exc:
            raise RuntimeError(f"Tesseract OCR failed: {exc}") from exc
    else:
        raise RuntimeError("OCR is required for image inputs; use --ocr auto|paddle|tesseract")

    for b in blocks:
        document.add(b)
    if not document.blocks:
        document.warn("No text detected in image.")
        document.add(Block("paragraph", "(no text detected)"))
    return document
