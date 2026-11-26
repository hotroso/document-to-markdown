# Document to Markdown

[![CI](https://github.com/hotroso/document-to-markdown/actions/workflows/ci.yml/badge.svg)](https://github.com/hotroso/document-to-markdown/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/hotroso/document-to-markdown?include_prereleases&sort=semver)](https://github.com/hotroso/document-to-markdown/releases)
[![Downloads](https://img.shields.io/github/downloads/hotroso/document-to-markdown/total)](https://github.com/hotroso/document-to-markdown/releases)
[![Python](https://img.shields.io/badge/python-%3E%3D3.10-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Stars](https://img.shields.io/github/stars/hotroso/document-to-markdown?style=social)](https://github.com/hotroso/document-to-markdown/stargazers)

![document-to-markdown banner](docs/social-preview.png)

Local, zero-config converter for **PDF**, **images**, and **Excel** files into clean, AI/RAG-ready **Markdown**.

Designed for personal AI, LLM, and RAG workflows where document layouts vary wildly and you want clean `.md` files out of the box without complex setup.

## Highlights

- **Clean Markdown by default**: Outputs only the `.md` file by default. Extracted PDF image folders (`_assets`) and JSON metadata sidecars (`.meta.json`) are opt-in via `--assets` and `--meta`.
- **Layout-aware PDF & Image conversion**: Combines native PDF text extraction (PyMuPDF) with automatic OCR on scanned or image-heavy pages, powered by **PP-Structure / PP-StructureV3** and **PaddleOCR** (with **Tesseract** fallback).
- **Simple multilingual OCR**: Defaults to `--lang vie`, which handles **Vietnamese, English, and Latin-script text** out of the box in PaddleOCR. Combined specifications such as `eng+vie` or `eng+jpn` automatically select the broader recognition model instead of dropping secondary languages.
- **Phone photo & scan friendly**: Automatically applies EXIF orientation correction, handles multi-page TIFFs, detects multi-column reading order, and removes repeated page headers/footers.
- **Smart Excel ingestion**: Splits multi-region worksheets into separate tables, expands merged cells, and resolves formula values via cached Excel values, headless LibreOffice recalculation, or optional pure-Python evaluation.
- **100% local**: Everything runs on your machine (PaddleOCR downloads models once on first run).

## Supported Formats

| Input | Extensions | Capabilities |
|-------|------------|--------------|
| **PDF** | `.pdf` | Native text extraction + automatic OCR on scanned/image-dominated pages, multi-column ordering, repeated header/footer removal, password prompt (`--password`), optional image export (`--assets`). |
| **Images** | `.png`, `.jpg`, `.jpeg`, `.webp`, `.bmp`, `.tif`, `.tiff` | EXIF auto-rotation, multi-frame TIFF support, layout/table analysis (PP-Structure) or line-grouped plain OCR, automatic Tesseract fallback. |
| **Excel** | `.xlsx`, `.xlsm` | Multi-region sheet splitting, merged cell handling, 3-tier formula evaluation (cached → LibreOffice → `formulas`), numeric column alignment. |

## Why `document-to-markdown`?

| Capability | `document-to-markdown` | `markitdown` | `pymupdf4llm` | `docling` |
|------------|:----------------------:|:------------:|:-------------:|:---------:|
| 100% local & offline | Yes | Yes | Yes | Yes |
| Hybrid native PDF + automatic scan OCR | Yes (PyMuPDF + PaddleOCR) | Basic / optional | Native PDF focus | Yes |
| PP-StructureV3 layout & table OCR | Yes (with Tesseract fallback) | No | No | Yes (DocLayNet) |
| Vietnamese + English multilingual OCR out of the box | Yes (`--lang vie`) | Limited | Native only | Depends on OCR engine |
| Configurable CPU/GPU OCR speed (`mobile` / `server`) | Yes (`--ocr-profile`) | No | N/A | Configurable |
| Multi-table Excel splitting + 3-tier formula evaluation | Yes | Basic sheet dump | No | Basic |
| Clean single `.md` output by default (opt-in assets/meta) | Yes | Yes | Yes | Configurable |
| Prebuilt standalone executables (Win / Linux / macOS) | Yes | No | No | No |

## Installation

Requires **Python >= 3.10**.

### Direct Install via `pip` (Recommended)

Install directly from GitHub in one command without cloning:

```bash
# Base install (native PDF + Excel)
pip install git+https://github.com/hotroso/document-to-markdown.git

# Recommended: add PaddleOCR + PP-Structure layout/table analysis
pip install "document-to-markdown[ocr] @ git+https://github.com/hotroso/document-to-markdown.git"

# Optional: Tesseract OCR fallback or Excel formula engine
pip install "document-to-markdown[tesseract] @ git+https://github.com/hotroso/document-to-markdown.git"
pip install "document-to-markdown[excel-calc] @ git+https://github.com/hotroso/document-to-markdown.git"

# Install everything
pip install "document-to-markdown[all] @ git+https://github.com/hotroso/document-to-markdown.git"
```

### From Local Source (Development)

```bash
git clone https://github.com/hotroso/document-to-markdown.git
cd document-to-markdown

pip install -e .
# or with optional extras: [ocr], [tesseract], [excel-calc], [all]
pip install -e ".[ocr]"
```

> **Note:** On the first OCR / structure run, PaddleOCR automatically downloads detection, recognition, and layout models (a few hundred MB).

For detailed workflows, troubleshooting, and examples, see the [Usage Guide](USAGE.md).

### Prebuilt Installers & Packages

Prebuilt Python packages (`.whl`, `.tar.gz`) and standalone executables (Linux, Windows `.exe`, macOS — bundling native PDF, Excel, and Tesseract OCR support) are automatically built by [GitHub Actions](.github/workflows/release.yml) and published on the [Releases page](https://github.com/hotroso/document-to-markdown/releases). For full PaddleOCR / PP-Structure support, install from source with `pip install -e ".[ocr]"`.

## Usage

After installation, you can run either `document-to-markdown` or `python -m document_converter.cli`.

```bash
# Convert a PDF (outputs report.md only)
document-to-markdown report.pdf

# Convert an image (auto-rotates EXIF, auto-selects PP-Structure / PaddleOCR)
document-to-markdown scan.png

# Convert a bilingual English + Vietnamese PDF with source comments for AI/RAG
document-to-markdown report.pdf --mode ai --lang eng+vie

# Export extracted PDF images (_assets/) and metadata sidecar (.meta.json)
document-to-markdown report.pdf --assets --meta

# Batch convert a whole folder
document-to-markdown data/input -o data/output --mode ai

# Convert an Excel workbook
document-to-markdown report.xlsx -o report.md

# Disable PP-Structure layout analysis (faster plain OCR only)
document-to-markdown complex.pdf --no-structure

# Use lightweight mobile PaddleOCR models (much faster on CPU)
document-to-markdown scan.pdf --ocr-profile mobile --ocr-version PP-OCRv5
```

### CLI Options

| Option | Values / Default | Description |
|--------|------------------|-------------|
| `inputs` | path(s) | Input file(s) or directory(ies) to convert recursively |
| `-o`, `--output` | auto (`<input>.md`) | Output `.md` file path or output directory |
| `--mode` | `balanced` (default), `fidelity`, `ai` | Markdown style (`fidelity`/`ai` include source location comments; `ai` suppresses inline warning blockquotes) |
| `--ocr` | `auto` (default), `paddle`, `tesseract`, `off` | OCR backend (`auto` prefers PaddleOCR / PP-Structure and falls back to Tesseract) |
| `--lang` | `vie` (default), `eng`, `eng+vie`, … | OCR language (`vie` covers Vietnamese + English/Latin in PaddleOCR) |
| `--ocr-profile` | `auto` (default), `mobile`, `server` | PaddleOCR model profile (`mobile` uses lightweight detection for much faster CPU inference) |
| `--ocr-version` | unset (Paddle default) | PaddleOCR model version (e.g. `PP-OCRv5`, `PP-OCRv4`, `PP-OCRv3`) |
| `--det-model` | unset | Explicit PaddleOCR text detection model name (e.g. `PP-OCRv5_mobile_det`) |
| `--rec-model` | unset | Explicit PaddleOCR text recognition model name (e.g. `latin_PP-OCRv5_mobile_rec`) |
| `--no-structure` | flag | Disable PP-Structure layout/table analysis and use plain OCR only |
| `--assets`, `--images` | flag | Export images extracted from PDF into `<stem>_assets/` |
| `--meta` | flag | Export machine-readable `<stem>.meta.json` metadata sidecar |
| `--preview` | flag | Convert only the first 3 pages of a PDF |
| `--password` | flag | Prompt for a password on encrypted PDFs |
| `--no-recalculate` | flag | Skip LibreOffice / pure-Python Excel formula recalculation |
| `--version` | flag | Print package version and exit |
| `-v`, `--verbose` | flag | Enable debug logging |

## Python API

```python
from document_converter import convert_file

output_md = convert_file(
    "report.pdf",
    "output/report.md",
    mode="balanced",       # "balanced" | "fidelity" | "ai"
    ocr="auto",            # "auto" | "paddle" | "tesseract" | "off"
    ocr_lang="vie",        # "vie", "eng", "eng+vie", ...
    ocr_profile="auto",    # "auto" | "mobile" | "server"
    ocr_version=None,      # e.g. "PP-OCRv5", "PP-OCRv4"
    det_model=None,        # e.g. "PP-OCRv5_mobile_det"
    rec_model=None,        # e.g. "latin_PP-OCRv5_mobile_rec"
    export_images=False,   # True to create output/report_assets/
    export_meta=False,     # True to create output/report.meta.json
)
```

## How It Works

### PDF & Image Layout Analysis
1. For PDFs in `--ocr auto` mode, pages with native text are extracted directly via PyMuPDF with multi-column clustering.
2. Scanned pages (little native text) or image-dominated pages (images covering `>= 35%` of the page) are automatically routed to OCR.
3. When PaddleOCR + PP-Structure is available (default), layout regions (titles, headings, paragraphs, lists, HTML tables) are reconstructed in reading order. Otherwise it falls back gracefully to line-grouped plain PaddleOCR or Tesseract.

### Excel Formula Evaluation (Multi-Tier)
When a workbook contains formulas, the converter resolves cell values in order:
1. **Cached values** already saved in the `.xlsx` file (fastest).
2. **LibreOffice** headless recalculation on a temporary copy (most compatible, requires `soffice` or `libreoffice` on `PATH`).
3. **Pure-Python** evaluation via the optional `formulas` package (`pip install -e ".[excel-calc]"`).
4. Retain the formula text and emit a conversion warning if no calculated value is available.

## Limitations

- Legacy `.xls` files are not supported directly (convert to `.xlsx` first, e.g. with LibreOffice).
- Heavily nested tables or handwritten forms remain challenging for local OCR.
- PP-Structure with high-DPI page rendering uses more CPU/RAM than plain native PDF text extraction.
- Pure-Python `formulas` coverage is partial for complex array formulas or Excel-specific functions; LibreOffice is recommended for formula-heavy workbooks.
- Mixing two completely different writing systems in a single PaddleOCR pass (e.g., Vietnamese + Japanese on the same page) is limited by single-model recognition dictionaries; use `--ocr tesseract` with system language packs when needed.

## Changelog

### 0.4.0
- Add optional PaddleOCR model selection across CLI and Python API while preserving defaults: `--ocr-profile` (`auto` | `mobile` | `server`), `--ocr-version` (e.g. `PP-OCRv5`, `PP-OCRv4`, `PP-OCRv3`), `--det-model`, and `--rec-model`
- Key cached OCR / PP-Structure engines by full model configuration and keep graceful fallbacks for `PPStructureV3` and PaddleOCR 2.x

### 0.3.3
- Fix `ValueError: The truth value of an array with more than one element is ambiguous` when reading NumPy array outputs (`rec_texts`, `rec_scores`, `rec_boxes`, `dt_polys`, `bbox`) from PaddleOCR 3.x / PP-Structure and normalize NumPy bounding boxes in `_bbox_rect`
- Add cross-platform PaddlePaddle 3.x PIR/oneDNN (`ConvertPirAttribute2RuntimeAttribute`) mitigation: proactively disable MKLDNN by default on Windows (`PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT=0` + `enable_mkldnn=False`, overridable via env), honor `FLAGS_use_mkldnn=0` on all platforms, and auto-retry with `enable_mkldnn=False` at runtime if the error occurs

### 0.3.2
- Fix PaddleOCR 3.x+ initialization by omitting the removed `show_log=False` constructor argument and silencing verbose logs via Python `logging`
- Preserve PaddleOCR 2.x constructor fallback (`use_angle_cls`, `show_log=False`, `use_gpu=False`) and handle both `TypeError` and `ValueError` fallbacks across `PaddleOCR`, `PPStructureV3`, and classic `PPStructure`

### 0.3.1
- Make `.md` the sole default output; opt in to `_assets` and `.meta.json` via `--assets` (`--images`) and `--meta`
- Default `--lang` to `vie` (covers Vietnamese + English/Latin in PaddleOCR) and preserve secondary non-English languages in multi-language specs like `eng+vie`
- Add `map_lang_for_tesseract` and shared Tesseract fallback for image inputs
- Fix `PPStructureV3` pre-formatted Markdown escaping and avoid promoting running page headers to headings
- Improve plain OCR visual-line sorting, dynamic paragraph spacing, and multi-column gutter detection
- Auto-apply EXIF orientation, support multi-frame TIFFs, and detect image-dominated PDF pages for OCR

### 0.3.0
- PP-Structure / PP-StructureV3 integration for layout, tables, and reading order
- Direct image file support
- Multi-column clustering for native PDF blocks and OCR lines
- `--no-structure` flag

### 0.2.0
- Primary OCR engine switched to PaddleOCR (Tesseract kept as fallback)
- Language mapping, lazy model loading, improved error recovery

### 0.1.0
- Initial foundation (PDF + Excel + Tesseract)

## License

Released under the [MIT License](LICENSE).

## Repository Stats

[![Star History Chart](https://api.star-history.com/svg?repos=hotroso/document-to-markdown&type=Date)](https://star-history.com/#hotroso/document-to-markdown&Date)
