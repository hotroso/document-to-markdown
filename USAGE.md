# Usage Guide

Detailed examples and practical workflows for **document-to-markdown**.

---

## 1. Command-Line Basics

You can invoke the converter using either the installed script or module form:

```bash
document-to-markdown <inputs...> [options]
# or
python -m document_converter.cli <inputs...> [options]
```

### Single File Conversion

By default, only a `<stem>.md` file is written next to the input (or to `-o/--output`):

```bash
# Writes report.md next to report.pdf
document-to-markdown report.pdf

# Writes to a custom .md path
document-to-markdown report.pdf -o out/summary.md

# Writes to a target directory (creates out/report.md)
document-to-markdown report.pdf -o out/
```

### Batch Directory Conversion

Pass one or more directories or multiple files. Directories are scanned recursively for supported extensions (`.pdf`, `.docx`, `.pptx`, `.xlsx`, `.xlsm`, `.eml`, `.msg`, `.html`, `.htm`, `.csv`, `.tsv`, `.png`, `.jpg`, `.jpeg`, `.webp`, `.bmp`, `.tif`, `.tiff`):

```bash
document-to-markdown data/input -o data/output
```

Multiple files or directories always treat `-o/--output` as an output directory.

---

## 2. Optional Exports (`--assets` & `--meta`)

Because Markdown is the primary output, auxiliary files are **disabled by default**. Enable them explicitly when needed:

```bash
# Export extracted PDF images into report_assets/ and link them in Markdown
document-to-markdown report.pdf --assets
# (--images is also accepted as an alias)

# Export machine-readable metadata sidecar (report.meta.json)
document-to-markdown report.pdf --meta

# Export both assets and metadata
document-to-markdown report.pdf -o out/report.md --assets --meta
```

### What `.meta.json` contains

When `--meta` is passed, `<output>.meta.json` includes:
- `source`, `source_type`, `title`
- `warnings` (e.g., removed repeated headers/footers, OCR fallbacks, unresolved Excel formulas)
- `metadata` (page count, resolved `ocr_engine`, `use_structure`, Excel sheet list, formula recalculation tier)
- `blocks` (per-block `kind`, `source` coordinates/page/sheet, `confidence`, `metadata`, `text_length`, `rows`, `columns`)

---

## 3. Markdown Styles (`--mode`)

Choose how much structural metadata is embedded in the `.md` output:

| Mode | Best for | Behavior |
|------|----------|----------|
| `balanced` (default) | Human reading & clean RAG chunks | Clean Markdown only; warnings (if any) shown in blockquotes and under `## Conversion warnings`. |
| `fidelity` | Auditing & traceability | Adds `<!-- source-file: ... -->` and per-block `<!-- source: page=1, bbox=[...] -->` HTML comments. |
| `ai` | LLM / RAG ingestion | Keeps `<!-- source: ... -->` traceability comments while suppressing inline `warning` blockquotes inside the body. |

```bash
document-to-markdown contract.pdf --mode ai
```

---

## 4. PDF Workflows

```bash
# Quick preview (first 3 pages only)
document-to-markdown large_book.pdf --preview

# Encrypted PDF (prompts interactively for password)
document-to-markdown protected.pdf --password

# Force OCR on all pages (ignore native PDF text)
document-to-markdown tricky_layout.pdf --ocr paddle

# Extract native PDF text only (skip OCR completely)
document-to-markdown digital_report.pdf --ocr off
```

In the default `--ocr auto` mode:
- Digital pages with native text and tables are extracted directly via `pdfplumber` and reordered across columns.
- Scanned pages (`< 40` native characters with images) or image-dominated pages (`< 250` native characters and images covering `>= 35%` of the page area) are automatically routed to OCR.
- Repeated top/bottom headers and footers across multi-page PDFs are automatically detected and stripped.

---

## 5. Image Workflows

Supported formats: `.png`, `.jpg`, `.jpeg`, `.webp`, `.bmp`, `.tif`, `.tiff`.

```bash
# Standard scan or phone photo (EXIF orientation corrected automatically)
document-to-markdown receipt.jpg

# Multi-page TIFF (inserts --- page breaks between frames)
document-to-markdown scanned_pack.tiff

# Faster plain OCR without PP-Structure layout/table reconstruction
document-to-markdown note.png --no-structure
```

---

## 6. OCR Engines & Multilingual Documents

### Choosing an Engine (`--ocr`)

- `auto` (default): Uses **PaddleOCR / PP-Structure** if installed, and automatically falls back to **Tesseract** if PaddleOCR is unavailable or fails on an image.
- `paddle`: Forces PaddleOCR / PP-Structure.
- `tesseract`: Forces Tesseract OCR (`pytesseract` + system `tesseract` binary).
- `off`: Disables OCR.

### Choosing Languages (`--lang`)

Default: `--lang vie`.

- **Vietnamese & English / Latin documents**: Leave the default `--lang vie` (or pass `--lang eng+vie`). PaddleOCR's `vi` model covers both Vietnamese diacritics and standard English/Latin characters in a single model.
- **English-only documents**: `--lang eng` (uses PaddleOCR's `en` model).
- **CJK + English documents**: `--lang jpn`, `--lang chi_sim`, `--lang chi_tra`, or `--lang kor` (also handles embedded ASCII/English).
- **Other European languages**: `--lang fra`, `--lang deu`, `--lang spa`, `--lang ita`, `--lang por`, `--lang rus`, `--lang tha`.
- **Mixed non-Latin scripts** (e.g., Vietnamese + Japanese on the same page): Use Tesseract with system language packs installed:
  ```bash
  document-to-markdown bilingual.pdf --ocr tesseract --lang vie+jpn
  ```

### Choosing PaddleOCR Models & Speed Profile (`--ocr-profile`, `--ocr-version`, `--det-model`, `--rec-model`)

By default (`--ocr-profile auto`), PaddleOCR uses its own default models. On CPU-only machines, PaddleOCR 3.x defaults to heavier `server` detection models — you can switch to lightweight `mobile` models for significantly faster inference:

```bash
# Fast CPU mode (uses PP-OCRv5_mobile_det while keeping the right script rec model for --lang)
document-to-markdown scan.pdf --ocr-profile mobile

# Pin a specific PaddleOCR generation (e.g. PP-OCRv5 or PP-OCRv4)
document-to-markdown scan.pdf --ocr-profile mobile --ocr-version PP-OCRv5

# Power-user explicit model overrides
document-to-markdown scan.pdf --det-model PP-OCRv5_mobile_det --rec-model latin_PP-OCRv5_mobile_rec
```

---

## 7. Office (Word, PowerPoint, Excel), Email, HTML & CSV

### Word (`.docx`) & PowerPoint (`.pptx`)

Works out of the box with zero extra dependencies:

```bash
# Convert a Word document (headings, lists, tables, hyperlinks, footnotes)
document-to-markdown proposal.docx

# Convert a PowerPoint deck (slides, tables, speaker notes) and export slide images
document-to-markdown pitch.pptx --assets
```

### Email (`.eml` & Outlook `.msg`)

`.eml` works out of the box; Outlook `.msg` requires the optional `[outlook]` extra (`pip install "document-to-markdown[outlook] @ git+https://github.com/hotroso/document-to-markdown.git"`):

```bash
document-to-markdown thread.eml --assets
document-to-markdown message.msg -o message.md
```

### HTML (`.html`, `.htm`) & CSV / TSV (`.csv`, `.tsv`)

```bash
document-to-markdown article.html -o article.md
document-to-markdown metrics.csv -o metrics.md
```

### Excel Workbooks (`.xlsx`, `.xlsm`)

Supported formats: `.xlsx`, `.xlsm` (legacy `.xls` must be converted to `.xlsx` first).

```bash
# Standard conversion (uses cached values, LibreOffice, or xlcalculator fallback)
document-to-markdown financials.xlsx -o financials.md

# Skip external LibreOffice / pure-Python formula recalculation
document-to-markdown financials.xlsx --no-recalculate
```

How Excel sheets are converted:
- Blank rows and columns split each worksheet into independent logical tables/regions.
- Merged cells are expanded so table rows remain aligned.
- Numeric columns (`>= 70%` numeric cells) are automatically right-aligned in Markdown (`---:`).

---

## 8. Python API

```python
from document_converter import convert_file

md_path = convert_file(
    input_path="docs/invoice.pdf",
    output_path="out/invoice.md",
    mode="ai",
    ocr="auto",
    ocr_lang="vie",
    export_images=False,
    export_meta=True,
    preview=False,
    password=None,
    recalculate=True,
    use_structure=True,
)
print(f"Written to {md_path}")
```

---

## 9. Troubleshooting

- **First run is slow / downloading models**: PaddleOCR and PP-Structure download their pretrained models once and cache them locally. Subsequent runs reuse the cached models.
- **`Tesseract OCR requires pytesseract` or missing language error**: Install `pip install -e ".[tesseract]"` and ensure `tesseract-ocr` plus the language pack (e.g., `tesseract-ocr-vie`) are installed on your system.
- **Excel formulas show `=...` instead of values**: The workbook had no cached formula results and neither LibreOffice (`soffice`/`libreoffice` on `PATH`) nor `formulas` (`pip install -e ".[excel-calc]"`) was available.
- **Verbose logs**: Add `-v` / `--verbose` to inspect OCR engine selection, structure fallbacks, and stack traces.
