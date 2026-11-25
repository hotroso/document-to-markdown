"""Conversion orchestration and machine-readable metadata export."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .excel_adapter import convert_excel
from .image_adapter import IMAGE_SUFFIXES, convert_image
from .markdown import render_document
from .pdf_adapter import convert_pdf


def _metadata(document) -> dict:
    return {
        "source": document.source,
        "source_type": document.source_type,
        "title": document.title,
        "warnings": document.warnings,
        "metadata": document.metadata,
        "blocks": [
            {
                "kind": block.kind,
                "source": block.source,
                "confidence": block.confidence,
                "metadata": block.metadata,
                "text_length": len(block.text),
                "rows": len(block.rows),
                "columns": max((len(row) for row in block.rows), default=0),
            }
            for block in document.blocks
        ],
    }


def convert_file(
    input_path: str | Path,
    output_path: str | Path,
    mode: str = "balanced",
    ocr: str = "auto",
    ocr_lang: str = "eng",
    export_images: bool = False,
    export_meta: bool = False,
    preview: bool = False,
    password: Optional[str] = None,
    recalculate: bool = True,
    use_structure: bool = True,
) -> Path:
    """Convert one PDF, image or Excel file to Markdown, optionally exporting assets and metadata."""
    source = Path(input_path)
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix.lower()
    assets = target.with_name(target.stem + "_assets") if export_images and suffix == ".pdf" else None

    if suffix == ".pdf":
        document = convert_pdf(
            source,
            assets,
            ocr=ocr,
            ocr_lang=ocr_lang,
            preview=preview,
            password=password,
            use_structure=use_structure,
        )
        if assets is not None:
            for block in document.blocks:
                if block.kind == "image":
                    block.text = assets.name + "/" + block.text
    elif suffix in IMAGE_SUFFIXES:
        document = convert_image(
            source,
            ocr=ocr,
            ocr_lang=ocr_lang,
            use_structure=use_structure,
        )
    elif suffix in {".xlsx", ".xlsm"}:
        document = convert_excel(source, recalculate=recalculate)
    elif suffix == ".xls":
        raise RuntimeError(
            "Legacy .xls is not supported; convert it to .xlsx first (e.g. with LibreOffice)."
        )
    else:
        raise RuntimeError(f"Unsupported input type: {source.suffix or '(no extension)'}")

    target.write_text(render_document(document, mode=mode), encoding="utf-8")
    if export_meta:
        target.with_suffix(".meta.json").write_text(
            json.dumps(_metadata(document), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return target
