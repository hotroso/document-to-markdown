# Document to Markdown

Local free-form converter for **PDF**, **images** (PNG/JPG/…) and **Excel** → AI-friendly Markdown.

Designed for personal AI / RAG workflows where layouts are not fixed in advance.

**Version 0.3** adds:

1. **PP-Structure / PP-StructureV3** layout analysis → better tables, headings and multi-column reading order
2. **Direct image support** (`.png`, `.jpg`, `.jpeg`, `.webp`, `.bmp`, `.tif`, `.tiff`)
3. **Improved multi-column ordering** for both native PDF text and OCR results

## Features

| Input | Capabilities |
|-------|--------------|
| **PDF** | Native text (PyMuPDF) + optional PP-Structure / PaddleOCR / Tesseract. Header/footer removal, image export, multi-column aware reading order. |
| **Image** | Full layout analysis (PP-Structure) or plain OCR → structured Markdown. |
| **Excel** | Multi-region sheets, merged cells, formula cached values + optional LibreOffice recalculation. |

Shared Markdown renderer (`balanced` / `fidelity` / `ai`) + `.meta.json` sidecar with coordinates & confidence.

Everything runs locally (PaddleOCR may download models on first use).

## Install

```bash
pip install -e .

# Recommended (PaddleOCR + PP-Structure)
pip install -e ".[ocr]"

# Optional Tesseract fallback
pip install -e ".[tesseract]"

# Everything
pip install -e ".[all]"
```

First PaddleOCR / structure run downloads detection, recognition and layout models (a few hundred MB).

## Usage

```bash
# PDF – auto (prefer structure + PaddleOCR)
python -m document_converter.cli report.pdf --mode ai --ocr auto

# Image → Markdown
python -m document_converter.cli scan.png --ocr paddle --lang vie

# Force structure off (plain OCR only)
python -m document_converter.cli complex.pdf --no-structure

# Directory batch
python -m document_converter.cli data/input --output data/output --mode ai --lang eng+vie

# Excel
python -m document_converter.cli report.xlsx -o report.md
```

### CLI options

| Option | Values / default | Meaning |
|--------|------------------|---------|
| `--ocr` | `auto` (default), `paddle`, `tesseract`, `off` | OCR backend |
| `--lang` | `eng`, `vie`, `eng+vie`, … | Language (mapped for Paddle) |
| `--no-structure` | flag | Disable PP-Structure layout/table analysis |
| `--mode` | `balanced`, `fidelity`, `ai` | Markdown style |
| `--preview` | flag | First 3 PDF pages only |
| `--no-images` | flag | Skip PDF image export |
| `--no-recalculate` | flag | Skip LibreOffice formula recalc |
| `-v` | flag | Verbose logging |

## How structure analysis helps

When `--ocr auto|paddle` and structure is available (default):

- Layout regions detected (title, paragraph title, text, table, figure, …)
- Tables reconstructed with cell structure (HTML → Markdown table)
- Multi-column reading order recovered
- Headings promoted more reliably than font-size heuristics alone

Falls back gracefully to plain PaddleOCR or native PDF text if structure models fail or are disabled.

## Excel formula evaluation (multi-tier)

When a workbook contains formulas the converter tries, in order:

1. **Cached values** already stored by Excel (fastest).
2. **LibreOffice** headless recalculation of a temporary copy (most compatible).
3. **Pure-Python** evaluation via the optional `formulas` package.
4. Keep the formula text itself and emit a warning for that cell.

Install the pure-Python engine:

```bash
pip install -e ".[excel-calc]"
# or
pip install formulas
```

LibreOffice (`soffice` / `libreoffice`) on PATH is still the most reliable for complex workbooks.

Disable external calculation:

```bash
python -m document_converter.cli report.xlsx --no-recalculate
```

## Limitations

- Legacy `.xls` not supported (convert to `.xlsx` first).
- Very complex nested tables / forms still imperfect.
- Structure + high-DPI rendering is slower and heavier on CPU/RAM than pure native extraction.
- Model download required once for PaddleOCR / PP-Structure.
- Pure-Python formula coverage is incomplete (complex array formulas, some Excel-only functions).

## Changelog

### 0.3.0
- PP-Structure / PP-StructureV3 integration for layout, tables and reading order
- Direct image file support
- Multi-column clustering for native PDF blocks and OCR lines
- `--no-structure` flag
- Version bump

### 0.2.0
- Primary OCR engine → PaddleOCR (Tesseract kept as fallback)
- Language mapping, lazy model loading, improved error recovery

### 0.1.0
- Initial rough foundation (PDF + Excel + Tesseract)
