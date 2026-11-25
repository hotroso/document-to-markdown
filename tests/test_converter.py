"""Unit tests for document_converter CLI, pipeline, Markdown renderer, and OCR helpers."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

from document_converter.cli import _parser, main
from document_converter.markdown import render_document
from document_converter.models import Block, Document
from document_converter.ocr_engine import (
    map_lang_for_paddle,
    map_lang_for_tesseract,
    ocr_pil_image_tesseract,
    run_plain_ocr_on_array,
    structure_result_to_blocks,
)
from document_converter.pdf_adapter import _page_needs_ocr


def test_language_mapping() -> None:
    assert map_lang_for_paddle("eng+vie") == "vi"
    assert map_lang_for_paddle("vie+eng") == "vi"
    assert map_lang_for_paddle("eng+jpn") == "japan"
    assert map_lang_for_paddle("eng") == "en"
    assert map_lang_for_paddle("") == "vi"

    assert map_lang_for_tesseract("vi") == "vie"
    assert map_lang_for_tesseract("en+vi") == "eng+vie"
    assert map_lang_for_tesseract("eng+vie") == "eng+vie"


def test_cli_defaults_and_opt_in_flags(tmp_path: Path) -> None:
    args = _parser().parse_args(["doc.pdf"])
    assert args.lang == "vie"
    assert args.export_images is False
    assert args.export_meta is False

    src = tmp_path / "sample.pdf"
    src.write_bytes(b"%PDF-1.4")

    def fake_pdf(path, output_assets=None, **kw):
        doc = Document(str(path), "pdf", path.stem)
        doc.add(Block("paragraph", "Hello world"))
        if output_assets is not None:
            output_assets.mkdir(parents=True, exist_ok=True)
            img = output_assets / "page-001-image-01.png"
            img.write_bytes(b"PNG")
            doc.add(Block("image", img.name, metadata={"alt": "img"}))
        return doc

    with patch("document_converter.pipeline.convert_pdf", side_effect=fake_pdf):
        out1 = tmp_path / "out1.md"
        assert main([str(src), "-o", str(out1)]) == 0
        assert out1.exists()
        assert not (tmp_path / "out1_assets").exists()
        assert not (tmp_path / "out1.meta.json").exists()

        out2 = tmp_path / "out2.md"
        assert main([str(src), "-o", str(out2), "--assets", "--meta"]) == 0
        assert out2.exists()
        assert (tmp_path / "out2_assets" / "page-001-image-01.png").exists()
        assert (tmp_path / "out2.meta.json").exists()

        out3 = tmp_path / "out3.md"
        assert main([str(src), "-o", str(out3), "--assets", "--no-images"]) == 0
        assert out3.exists()
        assert not (tmp_path / "out3_assets").exists()


def test_ppstructure_markdown_passthrough_and_header_handling() -> None:
    res = {"markdown": {"markdown_texts": "## Title\n\n**Bold** | A | B |"}}
    blocks = structure_result_to_blocks(res, page_number=1)
    doc = Document("x.pdf", "pdf", "x", blocks=blocks)
    md = render_document(doc)
    assert "## Title" in md
    assert "**Bold**" in md
    assert r"\#" not in md
    assert r"\*" not in md

    classic = [
        {"type": "header", "bbox": [0, 0, 100, 20], "res": "Running Header"},
        {"type": "title", "bbox": [0, 30, 100, 60], "res": "Main Heading"},
    ]
    cblocks = structure_result_to_blocks(classic, page_number=1)
    assert cblocks[0].kind == "paragraph"
    assert cblocks[1].kind == "heading"


def test_plain_ocr_line_sorting_and_multicolumn() -> None:
    fake_engine = MagicMock()
    del fake_engine.predict
    fake_engine.ocr.return_value = [[
        ([[120, 8], [200, 8], [200, 28], [120, 28]], ("World", 0.95)),
        ([[10, 10], [100, 10], [100, 30], [10, 30]], ("Hello", 0.90)),
        ([[10, 80], [150, 80], [150, 100], [10, 100]], ("Next para", 0.92)),
    ]]
    with patch("document_converter.ocr_engine.get_paddle_ocr", return_value=fake_engine):
        pblocks = run_plain_ocr_on_array(None, lang="eng+vie")
    assert [b.text for b in pblocks] == ["Hello World", "Next para"]
    assert "bbox" in pblocks[0].source

    fake_engine.ocr.return_value = [[
        ([[50, 10], [950, 10], [950, 40], [50, 40]], ("Full Title", 0.99)),
        ([[550, 70], [900, 70], [900, 90], [550, 90]], ("Right 1", 0.95)),
        ([[50, 70], [400, 70], [400, 90], [50, 90]], ("Left 1", 0.95)),
        ([[550, 100], [900, 100], [900, 120], [550, 120]], ("Right 2", 0.95)),
        ([[50, 100], [400, 100], [400, 120], [50, 120]], ("Left 2", 0.95)),
    ]]
    with patch("document_converter.ocr_engine.get_paddle_ocr", return_value=fake_engine):
        mblocks = run_plain_ocr_on_array(None, lang="vie")
    assert [b.text for b in mblocks] == ["Full Title", "Left 1 Left 2", "Right 1 Right 2"]


def test_page_needs_ocr_and_tesseract_lang() -> None:
    page = MagicMock()
    page.rect.width = 1000
    page.rect.height = 1000
    page.get_images.return_value = [(1,)]
    img_rect = MagicMock()
    img_rect.width = 800
    img_rect.height = 600
    page.get_image_rects.return_value = [img_rect]
    assert _page_needs_ocr(page, "Header text with 60 characters so native_text >= 40 chars here!") is True
    assert _page_needs_ocr(page, "x" * 300) is False

    fake_tess = types.SimpleNamespace(
        Output=types.SimpleNamespace(DICT="dict"),
        image_to_data=MagicMock(
            return_value={
                "text": ["Xin", "chào"],
                "block_num": [1, 1],
                "line_num": [1, 1],
                "top": [10, 10],
                "left": [10, 50],
                "conf": [90, 95],
            }
        ),
    )
    with patch.dict(sys.modules, {"pytesseract": fake_tess}):
        tblocks = ocr_pil_image_tesseract(MagicMock(), lang="en+vi", page_number=2)
    assert len(tblocks) == 1
    assert tblocks[0].text == "Xin chào"
    assert tblocks[0].source == {"page": 2}
    assert fake_tess.image_to_data.call_args.kwargs["lang"] == "eng+vie"
