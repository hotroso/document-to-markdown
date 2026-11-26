"""Unit tests for document_converter CLI, pipeline, Markdown renderer, and OCR helpers."""

from __future__ import annotations

import os
import sys
import types
import zipfile
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock, patch

from document_converter.cli import _parser, main
from document_converter.markdown import render_document
from document_converter.models import Block, Document
from document_converter import ocr_engine
from document_converter.ocr_engine import (
    get_paddle_ocr,
    get_structure_engine,
    map_lang_for_paddle,
    map_lang_for_tesseract,
    ocr_pil_image_tesseract,
    run_plain_ocr_on_array,
    run_structure_on_image,
    structure_result_to_blocks,
)
from document_converter.pdf_adapter import _page_blocks, _page_needs_ocr


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
    page = types.SimpleNamespace(
        width=1000.0,
        height=1000.0,
        images=[{"x0": 100, "top": 100, "x1": 900, "bottom": 700, "width": 800, "height": 600}],
    )
    assert _page_needs_ocr(page, "Header text with 60 characters so native_text >= 40 chars here!") is True
    assert _page_needs_ocr(page, "x" * 300) is False

    # Also test native pdfplumber _page_blocks extraction (heading + paragraph + ruled table)
    fake_table = MagicMock()
    fake_table.bbox = (50.0, 200.0, 500.0, 300.0)
    fake_table.extract.return_value = [["Col A", "Col B"], ["1", "2"]]
    fake_pdf_page = MagicMock()
    fake_pdf_page.width = 600.0
    fake_pdf_page.height = 800.0
    fake_pdf_page.find_tables.return_value = [fake_table]
    fake_pdf_page.extract_words.return_value = [
        {"text": "MAIN", "x0": 50.0, "top": 20.0, "x1": 120.0, "bottom": 42.0, "size": 18.0},
        {"text": "TITLE", "x0": 128.0, "top": 20.0, "x1": 200.0, "bottom": 42.0, "size": 18.0},
        {"text": "Body", "x0": 50.0, "top": 80.0, "x1": 90.0, "bottom": 92.0, "size": 11.0},
        {"text": "text", "x0": 95.0, "top": 80.0, "x1": 130.0, "bottom": 92.0, "size": 11.0},
        # Word inside table bbox should be ignored to avoid duplication
        {"text": "Col", "x0": 60.0, "top": 210.0, "x1": 90.0, "bottom": 225.0, "size": 11.0},
    ]
    pblocks = _page_blocks(fake_pdf_page, page_number=1)
    assert [b.kind for b in pblocks] == ["heading", "paragraph", "table"]
    assert pblocks[0].text == "MAIN TITLE"
    assert pblocks[1].text == "Body text"
    assert pblocks[2].rows == [["Col A", "Col B"], ["1", "2"]]

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


def _reset_ocr_singletons() -> None:
    ocr_engine._paddle_ocr = None
    ocr_engine._paddle_ocr_key = None
    ocr_engine._paddle_ocr_mkldnn_disabled = False
    ocr_engine._structure_engine = None
    ocr_engine._structure_key = None
    ocr_engine._structure_mkldnn_disabled = False
    ocr_engine._force_disable_mkldnn = False


def _clean_mkldnn_env():
    cleaned = {
        k: v
        for k, v in os.environ.items()
        if k not in {"PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "FLAGS_use_mkldnn"}
    }
    return patch.dict(os.environ, cleaned, clear=True)


def test_get_paddle_ocr_v3_omits_show_log() -> None:
    calls = []

    class FakePaddleOCRv3:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            if "show_log" in kwargs:
                raise ValueError("Unknown argument: show_log")

        def predict(self, img):
            return []

    fake_mod = types.SimpleNamespace(__version__="3.0.1", PaddleOCR=FakePaddleOCRv3)
    _reset_ocr_singletons()
    try:
        with (
            _clean_mkldnn_env(),
            patch.object(ocr_engine.sys, "platform", "linux"),
            patch.dict(sys.modules, {"paddleocr": fake_mod}),
        ):
            engine = get_paddle_ocr("vie")
        assert isinstance(engine, FakePaddleOCRv3)
        assert len(calls) == 1
        assert "show_log" not in calls[0]
        assert calls[0] == {
            "lang": "vi",
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
        }
    finally:
        _reset_ocr_singletons()


def test_get_paddle_ocr_v3_fallback_on_value_error_and_without_version() -> None:
    calls = []

    class FakeStrictPaddleOCRv3:
        def __init__(self, lang="en", **kwargs):
            calls.append({"lang": lang, **kwargs})
            if kwargs:
                raise ValueError(f"Unknown argument: {next(iter(kwargs))}")

        def predict(self, img):
            return []

    # No __version__ attribute -> detected via predict method
    fake_mod = types.SimpleNamespace(PaddleOCR=FakeStrictPaddleOCRv3)
    _reset_ocr_singletons()
    try:
        with (
            _clean_mkldnn_env(),
            patch.object(ocr_engine.sys, "platform", "linux"),
            patch.dict(sys.modules, {"paddleocr": fake_mod}),
        ):
            engine = get_paddle_ocr("eng")
        assert isinstance(engine, FakeStrictPaddleOCRv3)
        assert len(calls) == 2
        assert all("show_log" not in c for c in calls)
        assert calls[-1] == {"lang": "en"}
    finally:
        _reset_ocr_singletons()


def test_get_paddle_ocr_v2_keeps_show_log_fallback() -> None:
    calls = []

    class FakePaddleOCRv2:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def ocr(self, img, cls=True):
            return []

    fake_mod = types.SimpleNamespace(__version__="2.9.1", PaddleOCR=FakePaddleOCRv2)
    _reset_ocr_singletons()
    try:
        with (
            _clean_mkldnn_env(),
            patch.object(ocr_engine.sys, "platform", "linux"),
            patch.dict(sys.modules, {"paddleocr": fake_mod}),
        ):
            engine = get_paddle_ocr("vie")
        assert isinstance(engine, FakePaddleOCRv2)
        assert calls == [
            {"use_angle_cls": True, "lang": "vi", "show_log": False, "use_gpu": False}
        ]
    finally:
        _reset_ocr_singletons()

    # Legacy 2.x variant that rejects use_gpu or show_log with ValueError/TypeError
    calls.clear()

    class FakePaddleOCRv2NoShowLog:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            if "show_log" in kwargs:
                raise ValueError("Unknown argument: show_log")

        def ocr(self, img, cls=True):
            return []

    fake_mod2 = types.SimpleNamespace(__version__="2.8.1", PaddleOCR=FakePaddleOCRv2NoShowLog)
    _reset_ocr_singletons()
    try:
        with (
            _clean_mkldnn_env(),
            patch.object(ocr_engine.sys, "platform", "linux"),
            patch.dict(sys.modules, {"paddleocr": fake_mod2}),
        ):
            engine = get_paddle_ocr("eng")
        assert isinstance(engine, FakePaddleOCRv2NoShowLog)
        assert calls[-1] == {"use_angle_cls": True, "lang": "en"}
    finally:
        _reset_ocr_singletons()


def test_get_structure_engine_v3_and_v2_fallbacks() -> None:
    v3_calls = []

    class FakePPStructureV3:
        def __init__(self, **kwargs):
            v3_calls.append(kwargs)
            if "lang" in kwargs:
                raise ValueError("Unknown argument: lang")

    fake_v3 = types.SimpleNamespace(__version__="3.0.0", PPStructureV3=FakePPStructureV3)
    _reset_ocr_singletons()
    try:
        with (
            _clean_mkldnn_env(),
            patch.object(ocr_engine.sys, "platform", "linux"),
            patch.dict(sys.modules, {"paddleocr": fake_v3}),
        ):
            engine = get_structure_engine("vie")
        assert isinstance(engine, FakePPStructureV3)
        assert v3_calls == [{"lang": "vi"}, {}]
    finally:
        _reset_ocr_singletons()

    v2_calls = []

    class FakePPStructureV2:
        def __init__(self, **kwargs):
            v2_calls.append(kwargs)

    fake_v2 = types.SimpleNamespace(__version__="2.7.3", PPStructure=FakePPStructureV2)
    _reset_ocr_singletons()
    try:
        with (
            _clean_mkldnn_env(),
            patch.object(ocr_engine.sys, "platform", "linux"),
            patch.dict(sys.modules, {"paddleocr": fake_v2}),
        ):
            engine = get_structure_engine("vie")
        assert isinstance(engine, FakePPStructureV2)
        assert v2_calls == [{"show_log": False, "lang": "vi"}]
    finally:
        _reset_ocr_singletons()


def test_windows_disables_mkldnn_by_default_and_honors_env_overrides() -> None:
    ocr_calls = []
    struct_calls = []

    class FakePaddleOCRv3:
        def __init__(self, **kwargs):
            ocr_calls.append(kwargs)

        def predict(self, img):
            return []

    class FakePPStructureV3:
        def __init__(self, **kwargs):
            struct_calls.append(kwargs)

        def predict(self, img):
            return []

    fake_mod = types.SimpleNamespace(
        __version__="3.3.0",
        PaddleOCR=FakePaddleOCRv3,
        PPStructureV3=FakePPStructureV3,
    )

    # On Windows with no env overrides: proactively sets PaddleX env & passes enable_mkldnn=False
    _reset_ocr_singletons()
    try:
        with (
            _clean_mkldnn_env(),
            patch.object(ocr_engine.sys, "platform", "win32"),
            patch.dict(sys.modules, {"paddleocr": fake_mod}),
        ):
            get_paddle_ocr("vie")
            get_structure_engine("vie")
            assert os.environ.get("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT") == "0"
            assert os.environ.get("FLAGS_use_mkldnn") == "0"
        assert ocr_calls[-1]["enable_mkldnn"] is False
        assert struct_calls[-1] == {"lang": "vi", "enable_mkldnn": False}
    finally:
        _reset_ocr_singletons()

    # On Windows with explicit opt-in PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT=1: respects user override
    ocr_calls.clear()
    _reset_ocr_singletons()
    try:
        with (
            _clean_mkldnn_env(),
            patch.dict(os.environ, {"PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT": "1"}),
            patch.object(ocr_engine.sys, "platform", "win32"),
            patch.dict(sys.modules, {"paddleocr": fake_mod}),
        ):
            get_paddle_ocr("vie")
        assert "enable_mkldnn" not in ocr_calls[-1]
    finally:
        _reset_ocr_singletons()

    # On Linux with FLAGS_use_mkldnn=0: translates into enable_mkldnn=False for PaddleX
    ocr_calls.clear()
    _reset_ocr_singletons()
    try:
        with (
            _clean_mkldnn_env(),
            patch.dict(os.environ, {"FLAGS_use_mkldnn": "0"}),
            patch.object(ocr_engine.sys, "platform", "linux"),
            patch.dict(sys.modules, {"paddleocr": fake_mod}),
        ):
            get_paddle_ocr("vie")
            assert os.environ.get("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT") == "0"
        assert ocr_calls[-1]["enable_mkldnn"] is False
    finally:
        _reset_ocr_singletons()


def test_pir_mkldnn_runtime_auto_fallback_plain_and_structure() -> None:
    ocr_init_calls = []
    struct_init_calls = []

    class FlakyPaddleOCRv3:
        def __init__(self, **kwargs):
            ocr_init_calls.append(kwargs)
            self.mkldnn_off = kwargs.get("enable_mkldnn") is False

        def predict(self, img):
            if not self.mkldnn_off:
                raise NotImplementedError(
                    "(Unimplemented) ConvertPirAttribute2RuntimeAttribute not support "
                    "[pir::ArrayAttribute<pir::DoubleAttribute>]"
                )
            return [
                {
                    "rec_texts": ["Xin chào"],
                    "rec_scores": [0.98],
                    "rec_boxes": [[10, 10, 100, 30]],
                }
            ]

    class FlakyPPStructureV3:
        def __init__(self, **kwargs):
            struct_init_calls.append(kwargs)
            self.mkldnn_off = kwargs.get("enable_mkldnn") is False

        def predict(self, img):
            if not self.mkldnn_off:
                raise NotImplementedError(
                    "(Unimplemented) ConvertPirAttribute2RuntimeAttribute not support"
                )
            return [{"markdown": {"markdown_texts": "## OK"}}]

    fake_mod = types.SimpleNamespace(
        __version__="3.3.0",
        PaddleOCR=FlakyPaddleOCRv3,
        PPStructureV3=FlakyPPStructureV3,
    )

    _reset_ocr_singletons()
    try:
        with (
            _clean_mkldnn_env(),
            patch.object(ocr_engine.sys, "platform", "linux"),
            patch.dict(sys.modules, {"paddleocr": fake_mod}),
        ):
            blocks = run_plain_ocr_on_array(None, lang="vie")
            assert [b.text for b in blocks] == ["Xin chào"]
            assert len(ocr_init_calls) == 2
            assert "enable_mkldnn" not in ocr_init_calls[0]
            assert ocr_init_calls[1]["enable_mkldnn"] is False

            # Because _force_disable_mkldnn is now True, PPStructureV3 starts directly with enable_mkldnn=False
            sblocks = run_structure_on_image(None, lang="vie", page_number=1)
            assert [b.text for b in sblocks] == ["## OK"]
            assert struct_init_calls == [{"lang": "vi", "enable_mkldnn": False}]
    finally:
        _reset_ocr_singletons()

    # Also test PPStructureV3 hitting PIR/oneDNN error first and auto-retrying
    struct_init_calls.clear()
    _reset_ocr_singletons()
    try:
        with (
            _clean_mkldnn_env(),
            patch.object(ocr_engine.sys, "platform", "linux"),
            patch.dict(sys.modules, {"paddleocr": fake_mod}),
        ):
            sblocks = run_structure_on_image(None, lang="vie", page_number=1)
            assert [b.text for b in sblocks] == ["## OK"]
            assert struct_init_calls == [
                {"lang": "vi"},
                {"lang": "vi", "enable_mkldnn": False},
            ]
    finally:
        _reset_ocr_singletons()


class _FakeNdArray:
    """Mimics numpy.ndarray truthiness behavior (raises ValueError for >1 elements)."""

    def __init__(self, data):
        self._data = data
        self.size = self._count(data)

    @classmethod
    def _count(cls, x) -> int:
        if isinstance(x, list):
            return sum(cls._count(i) for i in x)
        return 1

    def __bool__(self) -> bool:
        if self.size == 0:
            return False
        if self.size == 1:
            return bool(self._data[0])
        raise ValueError(
            "The truth value of an array with more than one element is ambiguous. "
            "Use a.any() or a.all()"
        )

    def __len__(self) -> int:
        return len(self._data)

    def __iter__(self):
        for item in self._data:
            yield _FakeNdArray(item) if isinstance(item, list) else item

    def tolist(self):
        return self._data


def test_numpy_array_ocr_and_structure_results_do_not_use_truthiness() -> None:
    # 1. Dict-style PaddleOCR 3.x result with multi-element numpy-like arrays
    fake_engine_dict = MagicMock()
    fake_engine_dict.predict.return_value = [
        {
            "rec_texts": _FakeNdArray(["World", "Hello"]),
            "rec_scores": _FakeNdArray([0.95, 0.91]),
            "rec_boxes": _FakeNdArray([[120, 10, 200, 30], [10, 10, 100, 30]]),
        }
    ]
    with patch("document_converter.ocr_engine.get_paddle_ocr", return_value=fake_engine_dict):
        blocks = run_plain_ocr_on_array(None, lang="vie")
    assert [b.text for b in blocks] == ["Hello World"]
    assert blocks[0].source["bbox"] == [10.0, 10.0, 200.0, 30.0]

    # 2. Attribute-style result where rec_boxes is empty ndarray and dt_polys has polygons
    fake_res_attr = types.SimpleNamespace(
        rec_texts=_FakeNdArray(["Line 1", "Line 2"]),
        rec_scores=_FakeNdArray([0.92, 0.94]),
        rec_boxes=_FakeNdArray([]),
        dt_polys=_FakeNdArray(
            [
                [[10, 10], [100, 10], [100, 30], [10, 30]],
                [[10, 90], [100, 90], [100, 110], [10, 110]],
            ]
        ),
    )
    fake_engine_attr = MagicMock()
    fake_engine_attr.predict.return_value = [fake_res_attr]
    with patch("document_converter.ocr_engine.get_paddle_ocr", return_value=fake_engine_attr):
        ablocks = run_plain_ocr_on_array(None, lang="vie")
    assert [b.text for b in ablocks] == ["Line 1", "Line 2"]
    assert ablocks[0].source["bbox"] == [10.0, 10.0, 100.0, 30.0]
    assert ablocks[1].source["bbox"] == [10.0, 90.0, 100.0, 110.0]

    # 3. Structure result with numpy-like bbox arrays
    struct_items = [
        {"type": "text", "bbox": _FakeNdArray([0, 50, 100, 70]), "res": "Second"},
        {"type": "title", "bbox": _FakeNdArray([0, 10, 100, 30]), "res": "First"},
    ]
    sblocks = structure_result_to_blocks(struct_items, page_number=1)
    assert [b.text for b in sblocks] == ["First", "Second"]
    assert sblocks[0].source["bbox"] == [0.0, 10.0, 100.0, 30.0]


def test_paddle_model_selection_profiles_versions_and_cli(tmp_path: Path) -> None:
    # CLI defaults keep library default behavior untouched
    default_args = _parser().parse_args(["doc.pdf"])
    assert default_args.ocr_profile == "auto"
    assert default_args.ocr_version is None
    assert default_args.det_model is None
    assert default_args.rec_model is None

    # CLI passes model selection flags through convert_file -> convert_pdf
    src = tmp_path / "scan.pdf"
    src.write_bytes(b"%PDF-1.4")
    captured = {}

    def fake_pdf(path, output_assets=None, **kw):
        captured.update(kw)
        doc = Document(str(path), "pdf", path.stem)
        doc.add(Block("paragraph", "OK"))
        return doc

    with patch("document_converter.pipeline.convert_pdf", side_effect=fake_pdf):
        out = tmp_path / "scan.md"
        rc = main(
            [
                str(src),
                "-o",
                str(out),
                "--ocr-profile",
                "mobile",
                "--ocr-version",
                "PP-OCRv5",
                "--det-model",
                "PP-OCRv5_mobile_det",
                "--rec-model",
                "latin_PP-OCRv5_mobile_rec",
            ]
        )
    assert rc == 0
    assert captured["ocr_profile"] == "mobile"
    assert captured["ocr_version"] == "PP-OCRv5"
    assert captured["det_model"] == "PP-OCRv5_mobile_det"
    assert captured["rec_model"] == "latin_PP-OCRv5_mobile_rec"

    # PaddleOCR 3.x: mobile profile picks PP-OCRv5_mobile_det and preserves script rec for Vietnamese
    v3_calls = []

    class FakePaddleOCRv3:
        def __init__(self, **kwargs):
            v3_calls.append(kwargs)

        def predict(self, img):
            return []

    class FakePPStructureV3NoVersion:
        def __init__(self, **kwargs):
            v3_calls.append({"_cls": "PPStructureV3", **kwargs})
            if "ocr_version" in kwargs:
                raise ValueError("Unknown argument: ocr_version")

        def predict(self, img):
            return []

    fake_v3 = types.SimpleNamespace(
        __version__="3.1.0",
        PaddleOCR=FakePaddleOCRv3,
        PPStructureV3=FakePPStructureV3NoVersion,
    )
    _reset_ocr_singletons()
    try:
        with (
            _clean_mkldnn_env(),
            patch.object(ocr_engine.sys, "platform", "linux"),
            patch.dict(sys.modules, {"paddleocr": fake_v3}),
        ):
            e1 = get_paddle_ocr("vie", ocr_profile="mobile")
            assert v3_calls[-1]["text_detection_model_name"] == "PP-OCRv5_mobile_det"
            assert "text_recognition_model_name" not in v3_calls[-1]

            # Switching to server profile bypasses cache and uses server det
            e2 = get_paddle_ocr("vie", ocr_profile="server")
            assert e2 is not e1
            assert v3_calls[-1]["text_detection_model_name"] == "PP-OCRv5_server_det"

            # Chinese + mobile sets both mobile det & rec
            get_paddle_ocr("chi_sim", ocr_profile="mobile", ocr_version="PP-OCRv5")
            assert v3_calls[-1]["ocr_version"] == "PP-OCRv5"
            assert v3_calls[-1]["text_detection_model_name"] == "PP-OCRv5_mobile_det"
            assert v3_calls[-1]["text_recognition_model_name"] == "PP-OCRv5_mobile_rec"

            # Explicit det/rec override
            get_paddle_ocr(
                "vie",
                det_model="PP-OCRv4_mobile_det",
                rec_model="latin_PP-OCRv5_mobile_rec",
            )
            assert v3_calls[-1]["text_detection_model_name"] == "PP-OCRv4_mobile_det"
            assert v3_calls[-1]["text_recognition_model_name"] == "latin_PP-OCRv5_mobile_rec"

            # PPStructureV3 rejects ocr_version -> falls back keeping text_detection_model_name
            get_structure_engine("vie", ocr_profile="mobile", ocr_version="PP-OCRv5")
            assert v3_calls[-1] == {
                "_cls": "PPStructureV3",
                "lang": "vi",
                "text_detection_model_name": "PP-OCRv5_mobile_det",
            }
    finally:
        _reset_ocr_singletons()

    # PaddleOCR 2.x: passes ocr_version while keeping show_log=False
    v2_calls = []

    class FakePaddleOCRv2:
        def __init__(self, **kwargs):
            v2_calls.append(kwargs)

        def ocr(self, img, cls=True):
            return []

    fake_v2 = types.SimpleNamespace(__version__="2.9.1", PaddleOCR=FakePaddleOCRv2)
    _reset_ocr_singletons()
    try:
        with (
            _clean_mkldnn_env(),
            patch.object(ocr_engine.sys, "platform", "linux"),
            patch.dict(sys.modules, {"paddleocr": fake_v2}),
        ):
            get_paddle_ocr("vie", ocr_version="PP-OCRv4", ocr_profile="mobile")
        assert v2_calls == [
            {
                "use_angle_cls": True,
                "lang": "vi",
                "show_log": False,
                "use_gpu": False,
                "ocr_version": "PP-OCRv4",
            }
        ]
    finally:
        _reset_ocr_singletons()


def _make_sample_docx(dest: Path) -> None:
    core_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
                       xmlns:dc="http://purl.org/dc/elements/1.1/">
      <dc:title>Quarterly Plan</dc:title>
    </cp:coreProperties>"""

    styles_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:style w:type="paragraph" w:styleId="Heading1">
        <w:name w:val="heading 1"/>
        <w:pPr><w:outlineLvl w:val="0"/></w:pPr>
      </w:style>
    </w:styles>"""

    numbering_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:abstractNum w:abstractNumId="1">
        <w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/></w:lvl>
        <w:lvl w:ilvl="1"><w:numFmt w:val="bullet"/></w:lvl>
      </w:abstractNum>
      <w:num w:numId="1"><w:abstractNumId w:val="1"/></w:num>
    </w:numbering>"""

    rels_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.com" TargetMode="External"/>
      <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/img1.png"/>
    </Relationships>"""

    document_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
                xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
      <w:body>
        <w:p>
          <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
          <w:r><w:t>Overview</w:t></w:r>
        </w:p>
        <w:p>
          <w:r><w:t>Visit </w:t></w:r>
          <w:hyperlink r:id="rId1"><w:r><w:t>our site</w:t></w:r></w:hyperlink>
          <w:del><w:r><w:delText>OLD</w:delText></w:r></w:del>
        </w:p>
        <w:p>
          <w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>
          <w:r><w:t>First step</w:t></w:r>
        </w:p>
        <w:p>
          <w:pPr><w:numPr><w:ilvl w:val="1"/><w:numId w:val="1"/></w:numPr></w:pPr>
          <w:r><w:t>Sub detail</w:t></w:r>
        </w:p>
        <w:tbl>
          <w:tr>
            <w:tc><w:p><w:r><w:t>Item</w:t></w:r></w:p></w:tc>
            <w:tc><w:p><w:r><w:t>Price</w:t></w:r></w:p></w:tc>
          </w:tr>
          <w:tr>
            <w:tc><w:p><w:r><w:t>Widget</w:t></w:r></w:p></w:tc>
            <w:tc><w:p><w:r><w:t>120</w:t></w:r></w:p></w:tc>
          </w:tr>
        </w:tbl>
        <w:p>
          <w:r><w:drawing><a:blip r:embed="rId2"/></w:drawing></w:r>
        </w:p>
      </w:body>
    </w:document>"""

    footnotes_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:t>---</w:t></w:r></w:p></w:footnote>
      <w:footnote w:id="1"><w:p><w:r><w:t>Verified in Q1.</w:t></w:r></w:p></w:footnote>
    </w:footnotes>"""

    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("docProps/core.xml", core_xml)
        zf.writestr("word/styles.xml", styles_xml)
        zf.writestr("word/numbering.xml", numbering_xml)
        zf.writestr("word/_rels/document.xml.rels", rels_xml)
        zf.writestr("word/document.xml", document_xml)
        zf.writestr("word/footnotes.xml", footnotes_xml)
        zf.writestr("word/media/img1.png", b"\x89PNG\r\n\x1a\n")


def _make_sample_pptx(dest: Path) -> None:
    pres_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
                    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <p:sldIdLst>
        <p:sldId id="256" r:id="rId2"/>
        <p:sldId id="257" r:id="rId1"/>
      </p:sldIdLst>
    </p:presentation>"""

    pres_rels = """<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide2.xml"/>
      <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/>
    </Relationships>"""

    slide1_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
           xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
      <p:cSld><p:spTree>
        <p:sp>
          <p:nvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>
          <p:spPr><a:xfrm><a:off x="0" y="10"/></a:xfrm></p:spPr>
          <p:txBody><a:p><a:r><a:t>Intro Deck</a:t></a:r></a:p></p:txBody>
        </p:sp>
        <p:sp>
          <p:nvSpPr><p:nvPr><p:ph type="body"/></p:nvPr></p:nvSpPr>
          <p:spPr><a:xfrm><a:off x="0" y="100"/></a:xfrm></p:spPr>
          <p:txBody>
            <a:p><a:r><a:t>Fast local OCR</a:t></a:r></a:p>
            <a:p><a:pPr lvl="1"/><a:r><a:t>Supports Vietnamese</a:t></a:r></a:p>
          </p:txBody>
        </p:sp>
      </p:spTree></p:cSld>
    </p:sld>"""

    slide1_rels = """<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide" Target="../notesSlides/notesSlide1.xml"/>
    </Relationships>"""

    notes1_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <p:notes xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
             xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
      <p:cSld><p:spTree>
        <p:sp>
          <p:nvSpPr><p:nvPr><p:ph type="body"/></p:nvPr></p:nvSpPr>
          <p:txBody><a:p><a:r><a:t>Remember to mention offline privacy.</a:t></a:r></a:p></p:txBody>
        </p:sp>
      </p:spTree></p:cSld>
    </p:notes>"""

    slide2_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
           xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
      <p:cSld><p:spTree>
        <p:sp>
          <p:nvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>
          <p:txBody><a:p><a:r><a:t>Metrics</a:t></a:r></a:p></p:txBody>
        </p:sp>
        <p:graphicFrame>
          <a:graphic><a:graphicData><a:tbl>
            <a:tr>
              <a:tc><a:txBody><a:p><a:r><a:t>Metric</a:t></a:r></a:p></a:txBody></a:tc>
              <a:tc><a:txBody><a:p><a:r><a:t>Score</a:t></a:r></a:p></a:txBody></a:tc>
            </a:tr>
            <a:tr>
              <a:tc><a:txBody><a:p><a:r><a:t>Accuracy</a:t></a:r></a:p></a:txBody></a:tc>
              <a:tc><a:txBody><a:p><a:r><a:t>99%</a:t></a:r></a:p></a:txBody></a:tc>
            </a:tr>
          </a:tbl></a:graphicData></a:graphic>
        </p:graphicFrame>
      </p:spTree></p:cSld>
    </p:sld>"""

    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("ppt/presentation.xml", pres_xml)
        zf.writestr("ppt/_rels/presentation.xml.rels", pres_rels)
        zf.writestr("ppt/slides/slide1.xml", slide1_xml)
        zf.writestr("ppt/slides/_rels/slide1.xml.rels", slide1_rels)
        zf.writestr("ppt/notesSlides/notesSlide1.xml", notes1_xml)
        zf.writestr("ppt/slides/slide2.xml", slide2_xml)


def test_docx_and_pptx_adapters_end_to_end(tmp_path: Path) -> None:
    docx_file = tmp_path / "plan.docx"
    _make_sample_docx(docx_file)
    out_docx = tmp_path / "plan.md"
    assert main([str(docx_file), "-o", str(out_docx), "--assets"]) == 0
    md = out_docx.read_text(encoding="utf-8")
    assert "# Quarterly Plan" in md
    assert "# Overview" in md
    assert "our site (https://example.com)" in md
    assert "OLD" not in md
    assert "1. First step" in md
    assert "  - Sub detail" in md
    assert "| Item | Price |" in md
    assert "| Widget | 120 |" in md
    assert "## Footnotes" in md
    assert "Verified in Q1." in md
    assert (tmp_path / "plan_assets" / "docx-image-01.png").exists()
    assert "plan_assets/docx-image-01.png" in md

    pptx_file = tmp_path / "deck.pptx"
    _make_sample_pptx(pptx_file)
    out_pptx = tmp_path / "deck.md"
    assert main([str(pptx_file), "-o", str(out_pptx)]) == 0
    pmd = out_pptx.read_text(encoding="utf-8")
    assert "## Slide 1: Intro Deck" in pmd
    assert "- Fast local OCR" in pmd
    assert "  - Supports Vietnamese" in pmd
    assert "### Speaker Notes" in pmd
    assert "Remember to mention offline privacy." in pmd
    assert "## Slide 2: Metrics" in pmd
    assert "| Accuracy | 99% |" in pmd
    assert pmd.index("Slide 1") < pmd.index("Slide 2")


def test_email_html_and_csv_adapters_end_to_end(tmp_path: Path) -> None:
    # 1. Multipart .eml with attachment
    eml_file = tmp_path / "memo.eml"
    msg = EmailMessage()
    msg["Subject"] = "Q4 Sync"
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    msg["Date"] = "Thu, 26 Nov 2026 10:00:00 +0700"
    msg.set_content("Hello Bob,\n\n> Previous note\n\nSee attached.")
    msg.add_attachment(b"PNGDATA", maintype="image", subtype="png", filename="chart.png")
    eml_file.write_bytes(msg.as_bytes())

    out_eml = tmp_path / "memo.md"
    assert main([str(eml_file), "-o", str(out_eml), "--assets"]) == 0
    emd = out_eml.read_text(encoding="utf-8")
    assert "# Q4 Sync" in emd
    assert "alice@example.com" in emd
    assert "> Previous note" in emd
    assert "## Attachments" in emd
    assert "chart.png" in emd
    assert (tmp_path / "memo_assets" / "chart.png").exists()

    # 2. Outlook .msg via mocked BSD olefile
    msg_file = tmp_path / "outlook.msg"
    msg_file.write_bytes(b"FAKE_OLE_MSG")

    class _FakeStream:
        def __init__(self, data: bytes):
            self._data = data

        def read(self) -> bytes:
            return self._data

    class FakeOleFileIO:
        _streams = {
            ("__substg1.0_0037001F",): "Outlook Update".encode("utf-16-le"),
            ("__substg1.0_0C1A001F",): "CTO".encode("utf-16-le"),
            ("__substg1.0_0C1F001F",): "cto@example.com".encode("utf-16-le"),
            ("__substg1.0_0E04001F",): "team@example.com".encode("utf-16-le"),
            ("__substg1.0_1000001F",): "Release is live.".encode("utf-16-le"),
            ("__substg1.0_10130102",): b"<table><tr><th>Env</th><th>Status</th></tr><tr><td>Prod</td><td>OK</td></tr></table>",
            ("__attach_version1.0_#00000000", "__substg1.0_3707001F"): "spec.pdf".encode("utf-16-le"),
            ("__attach_version1.0_#00000000", "__substg1.0_370E001F"): "application/pdf".encode("utf-16-le"),
            ("__attach_version1.0_#00000000", "__substg1.0_37010102"): b"%PDF-1.4",
        }

        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def exists(self, parts):
            return tuple(parts) in self._streams

        def openstream(self, parts):
            return _FakeStream(self._streams[tuple(parts)])

        def listdir(self, streams=True, storages=False):
            return [list(k) for k in self._streams]

    fake_olefile = types.SimpleNamespace(OleFileIO=FakeOleFileIO)
    out_msg = tmp_path / "outlook.md"
    with patch.dict(sys.modules, {"olefile": fake_olefile}):
        assert main([str(msg_file), "-o", str(out_msg)]) == 0
    mmd = out_msg.read_text(encoding="utf-8")
    assert "# Outlook Update" in mmd
    assert "CTO" in mmd
    assert "cto@example.com" in mmd
    assert "| Prod | OK |" in mmd
    assert "spec.pdf" in mmd

    # 3. HTML with script stripped, code block, blockquote, colspan table
    html_file = tmp_path / "page.html"
    html_file.write_text(
        """<!doctype html>
        <html><head><title>Web Doc</title><style>body{color:red}</style></head>
        <body>
          <script>alert('ignore')</script>
          <h2>Section A</h2>
          <p>Intro paragraph.</p>
          <blockquote>Quoted insight</blockquote>
          <pre><code class="language-python">x = 1 + 2</code></pre>
          <table>
            <tr><th>Name</th><th colspan="2">Scores</th></tr>
            <tr><td>Alice</td><td>10</td><td>20</td></tr>
          </table>
        </body></html>""",
        encoding="utf-8",
    )
    out_html = tmp_path / "page.md"
    assert main([str(html_file), "-o", str(out_html)]) == 0
    hmd = out_html.read_text(encoding="utf-8")
    assert "# Web Doc" in hmd
    assert "## Section A" in hmd
    assert "alert" not in hmd
    assert "> Quoted insight" in hmd
    assert "```python\nx = 1 + 2\n```" in hmd
    assert "| Alice | 10 | 20 |" in hmd

    # 4. CSV (semicolon-delimited) and TSV
    csv_file = tmp_path / "data.csv"
    csv_file.write_text("Name;Amount\nAlpha;100\nBeta;250\n", encoding="utf-8-sig")
    out_csv = tmp_path / "data.md"
    assert main([str(csv_file), "-o", str(out_csv)]) == 0
    cmd = out_csv.read_text(encoding="utf-8")
    assert "| Name | Amount |" in cmd
    assert "| :--- | ---: |" in cmd
    assert "| Alpha | 100 |" in cmd

    tsv_file = tmp_path / "data.tsv"
    tsv_file.write_text("Key\tValue\nfoo\tbar\n", encoding="utf-8")
    out_tsv = tmp_path / "data_tsv.md"
    assert main([str(tsv_file), "-o", str(out_tsv)]) == 0
    assert "| foo | bar |" in out_tsv.read_text(encoding="utf-8")


def test_lazy_ocr_detection_does_not_import_paddle_for_digital_pdf(tmp_path: Path) -> None:
    from document_converter.ocr_engine import detect_ocr_engine
    from document_converter.pdf_adapter import convert_pdf

    # 1. detect_ocr_engine uses find_spec without importing paddleocr
    saved_paddle = sys.modules.pop("paddleocr", None)
    try:
        with patch("document_converter.ocr_engine.importlib.util.find_spec", side_effect=lambda n: object() if n == "paddleocr" else None):
            assert detect_ocr_engine("auto") == "paddle"
            assert "paddleocr" not in sys.modules
    finally:
        if saved_paddle is not None:
            sys.modules["paddleocr"] = saved_paddle

    # 2. Digital PDF with no scanned pages never calls detect_ocr_engine or opens pypdfium2
    fake_page = MagicMock()
    fake_page.width = 600.0
    fake_page.height = 800.0
    fake_page.images = []
    fake_page.find_tables.return_value = []
    fake_page.extract_words.return_value = [
        {"text": "Hello", "x0": 50.0, "top": 50.0, "x1": 100.0, "bottom": 62.0, "size": 11.0},
        {"text": "digital", "x0": 105.0, "top": 50.0, "x1": 160.0, "bottom": 62.0, "size": 11.0},
        {"text": "PDF", "x0": 165.0, "top": 50.0, "x1": 200.0, "bottom": 62.0, "size": 11.0},
    ]
    fake_pdf = MagicMock()
    fake_pdf.pages = [fake_page]
    fake_pdfplumber = types.SimpleNamespace(open=MagicMock(return_value=fake_pdf))

    dummy_pdf = tmp_path / "digital.pdf"
    dummy_pdf.write_bytes(b"%PDF-1.4")
    with (
        patch.dict(sys.modules, {"pdfplumber": fake_pdfplumber}),
        patch("document_converter.pdf_adapter.detect_ocr_engine", side_effect=AssertionError("should not be called")) as mock_detect,
    ):
        doc = convert_pdf(dummy_pdf, ocr="auto")
    assert mock_detect.call_count == 0
    assert doc.metadata["ocr_engine"] == "not_needed"
    assert [b.text for b in doc.blocks] == ["Hello digital PDF"]
