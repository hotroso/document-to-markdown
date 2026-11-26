"""Conversion orchestration and machine-readable metadata export."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .csv_adapter import CSV_SUFFIXES, convert_csv
from .docx_adapter import DOCX_SUFFIXES, convert_docx
from .email_adapter import EMAIL_SUFFIXES, convert_email
from .excel_adapter import convert_excel
from .html_adapter import HTML_SUFFIXES, convert_html
from .image_adapter import IMAGE_SUFFIXES, convert_image
from .markdown import render_document
from .pdf_adapter import convert_pdf
from .pptx_adapter import PPTX_SUFFIXES, convert_pptx

ASSET_EXPORT_SUFFIXES = {".pdf"} | DOCX_SUFFIXES | PPTX_SUFFIXES | EMAIL_SUFFIXES
SUPPORTED_SUFFIXES = (
    {".pdf", ".xlsx", ".xlsm"}
    | IMAGE_SUFFIXES
    | DOCX_SUFFIXES
    | PPTX_SUFFIXES
    | EMAIL_SUFFIXES
    | HTML_SUFFIXES
    | CSV_SUFFIXES
)


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
    ocr_lang: str = "vie",
    export_images: bool = False,
    export_meta: bool = False,
    preview: bool = False,
    password: Optional[str] = None,
    recalculate: bool = True,
    use_structure: bool = True,
    ocr_version: Optional[str] = None,
    ocr_profile: str = "auto",
    det_model: Optional[str] = None,
    rec_model: Optional[str] = None,
) -> Path:
    """Convert one supported document (PDF, Office, Email, HTML, CSV, image) to Markdown."""
    source = Path(input_path)
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix.lower()
    assets = (
        target.with_name(target.stem + "_assets")
        if export_images and suffix in ASSET_EXPORT_SUFFIXES
        else None
    )

    if suffix == ".pdf":
        document = convert_pdf(
            source,
            assets,
            ocr=ocr,
            ocr_lang=ocr_lang,
            preview=preview,
            password=password,
            use_structure=use_structure,
            ocr_version=ocr_version,
            ocr_profile=ocr_profile,
            det_model=det_model,
            rec_model=rec_model,
        )
    elif suffix in IMAGE_SUFFIXES:
        document = convert_image(
            source,
            ocr=ocr,
            ocr_lang=ocr_lang,
            use_structure=use_structure,
            ocr_version=ocr_version,
            ocr_profile=ocr_profile,
            det_model=det_model,
            rec_model=rec_model,
        )
    elif suffix in DOCX_SUFFIXES:
        document = convert_docx(source, output_assets=assets)
    elif suffix in PPTX_SUFFIXES:
        document = convert_pptx(source, output_assets=assets)
    elif suffix in EMAIL_SUFFIXES:
        document = convert_email(source, output_assets=assets)
    elif suffix in HTML_SUFFIXES:
        document = convert_html(source)
    elif suffix in CSV_SUFFIXES:
        document = convert_csv(source)
    elif suffix in {".xlsx", ".xlsm"}:
        document = convert_excel(source, recalculate=recalculate)
    elif suffix in {".xls", ".doc", ".ppt"}:
        modern = {".xls": ".xlsx", ".doc": ".docx", ".ppt": ".pptx"}[suffix]
        raise RuntimeError(
            f"Legacy {suffix} is not supported directly; convert it to {modern} first (e.g. with LibreOffice)."
        )
    else:
        raise RuntimeError(f"Unsupported input type: {source.suffix or '(no extension)'}")

    if assets is not None:
        for block in document.blocks:
            if block.kind == "image" and not block.text.startswith(("http://", "https://", assets.name + "/")):
                block.text = assets.name + "/" + block.text

    target.write_text(render_document(document, mode=mode), encoding="utf-8")
    if export_meta:
        target.with_suffix(".meta.json").write_text(
            json.dumps(_metadata(document), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return target
