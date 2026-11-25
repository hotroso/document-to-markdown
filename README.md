# Document to Markdown

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

## Installation

Requires **Python >= 3.10**.

```bash
git clone https://github.com/sinhsac/document-to-markdown.git
cd document-to-markdown

# Base install (native PDF + Excel)
pip install -e .

# Recommended: add PaddleOCR + PP-Structure layout/table analysis
pip install -e ".[ocr]"

# Optional: Tesseract OCR fallback
pip install -e ".[tesseract]"

# Optional: pure-Python Excel formula evaluation
pip install -e ".[excel-calc]"

# Install everything
pip install -e ".[all]"
```

> **Note:** On the first OCR / structure run, PaddleOCR automatically downloads detection, recognition, and layout models (a few hundred MB).

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
```

### CLI Options

| Option | Values / Default | Description |
|--------|------------------|-------------|
| `inputs` | path(s) | Input file(s) or directory(ies) to convert recursively |
| `-o`, `--output` | auto (`<input>.md`) | Output `.md` file path or output directory |
| `--mode` | `balanced` (default), `fidelity`, `ai` | Markdown style (`fidelity`/`ai` include source location comments; `ai` suppresses inline warning blockquotes) |
| `--ocr` | `auto` (default), `paddle`, `tesseract`, `off` | OCR backend (`auto` prefers PaddleOCR / PP-Structure and falls back to Tesseract) |
| `--lang` | `vie` (default), `eng`, `eng+vie`, … | OCR language (`vie` covers Vietnamese + English/Latin in PaddleOCR) |
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
    mode="balanced",      # "balanced" | "fidelity" | "ai"
    ocr="auto",           # "auto" | "paddle" | "tesseract" | "off"
    ocr_lang="vie",       # "vie", "eng", "eng+vie", ...
    export_images=False,  # True to create output/report_assets/
    export_meta=False,    # True to create output/report.meta.json
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
