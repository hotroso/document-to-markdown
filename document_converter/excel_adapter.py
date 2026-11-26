"""Free-form Excel ingestion with multi-tier formula evaluation."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from datetime import date, datetime, time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .models import Block, Document

logger = logging.getLogger(__name__)


def _display(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value).strip()


def _merged_values(ws) -> Dict[Tuple[int, int], object]:
    result: Dict[Tuple[int, int], object] = {}
    for merged in ws.merged_cells.ranges:
        top = ws.cell(merged.min_row, merged.min_col).value
        for row in range(merged.min_row, merged.max_row + 1):
            for col in range(merged.min_col, merged.max_col + 1):
                result[(row, col)] = top
    return result


def _used_bounds(ws) -> Optional[Tuple[int, int, int, int]]:
    cells = [cell for row in ws.iter_rows() for cell in row if cell.value is not None]
    if not cells:
        return None
    return (
        min(c.row for c in cells),
        max(c.row for c in cells),
        min(c.column for c in cells),
        max(c.column for c in cells),
    )


def _has_values(ws, row: int, left: int, right: int, merged: Dict[Tuple[int, int], object]) -> bool:
    return any(
        ws.cell(row, col).value is not None or (row, col) in merged
        for col in range(left, right + 1)
    )


def _split_row_bands(
    ws, top: int, bottom: int, left: int, right: int, merged: Dict[Tuple[int, int], object]
) -> List[Tuple[int, int]]:
    bands: List[Tuple[int, int]] = []
    start = None
    for row in range(top, bottom + 1):
        present = _has_values(ws, row, left, right, merged)
        if present and start is None:
            start = row
        elif not present and start is not None:
            bands.append((start, row - 1))
            start = None
    if start is not None:
        bands.append((start, bottom))
    return bands


def _split_column_ranges(
    ws, top: int, bottom: int, left: int, right: int, merged: Dict[Tuple[int, int], object]
) -> List[Tuple[int, int]]:
    ranges: List[Tuple[int, int]] = []
    start = None
    for col in range(left, right + 1):
        present = any(
            ws.cell(row, col).value is not None or (row, col) in merged
            for row in range(top, bottom + 1)
        )
        if present and start is None:
            start = col
        elif not present and start is not None:
            ranges.append((start, col - 1))
            start = None
    if start is not None:
        ranges.append((start, right))
    return ranges


def _workbook_has_formulas(workbook) -> bool:
    return any(
        isinstance(cell.value, str) and cell.value.startswith("=")
        for ws in workbook.worksheets
        for row in ws.iter_rows()
        for cell in row
    )


def _recalculate_with_libreoffice(path: Path, temp_dir: Path, document: Document) -> Optional[Path]:
    """Force LibreOffice to recalculate formulas into a temporary xlsx copy."""
    binary = shutil.which("soffice") or shutil.which("libreoffice")
    if not binary:
        return None

    input_dir = temp_dir / "lo_input"
    output_dir = temp_dir / "lo_output"
    profile_dir = temp_dir / "lo_profile"
    input_dir.mkdir()
    output_dir.mkdir()
    profile_dir.mkdir()

    copied = input_dir / path.name
    shutil.copy2(path, copied)

    # Use a clean user profile so calculation settings are consistent.
    # --calc is not always needed; converting to xlsx often triggers evaluation.
    command = [
        binary,
        "--headless",
        "--norestore",
        "--nofirststartwizard",
        f"-env:UserInstallation=file://{profile_dir.resolve()}",
        "--convert-to",
        "xlsx",
        "--outdir",
        str(output_dir),
        str(copied),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )
    except subprocess.TimeoutExpired:
        document.warn("LibreOffice formula recalculation timed out; trying other methods.")
        return None
    except Exception as exc:
        document.warn(f"LibreOffice recalculation failed to start ({exc}).")
        return None

    calculated = output_dir / f"{path.stem}.xlsx"
    if result.returncode == 0 and calculated.exists():
        document.metadata["formula_recalculation"] = "libreoffice"
        logger.info("LibreOffice recalculation succeeded for %s", path.name)
        return calculated

    detail = (result.stderr or result.stdout or "unknown error").strip().splitlines()
    detail = detail[0] if detail else "unknown LibreOffice error"
    document.warn(f"LibreOffice recalculation failed ({detail}).")
    return None


def _evaluate_with_xlcalculator(path: Path, document: Document):
    """Pure-Python evaluation using MIT-licensed `xlcalculator` (optional dependency)."""
    try:
        from xlcalculator import Evaluator, ModelCompiler
    except ImportError:
        return None

    try:
        compiler = ModelCompiler()
        model = compiler.read_and_parse_archive(str(path))
        evaluator = Evaluator(model)
        document.metadata["formula_recalculation"] = "xlcalculator"
        logger.info("Loaded xlcalculator engine for %s", path.name)
        return evaluator
    except Exception as exc:
        document.warn(f"Pure-Python formula engine (xlcalculator) failed: {exc}")
        return None


def _cell_value_from_evaluator(evaluator, sheet_name: str, row: int, col: int) -> Optional[object]:
    """Evaluate a single cell using an xlcalculator Evaluator."""
    try:
        from openpyxl.utils import get_column_letter

        col_letter = get_column_letter(col)
        for addr in (f"{sheet_name}!{col_letter}{row}", f"'{sheet_name}'!{col_letter}{row}"):
            try:
                val = evaluator.evaluate(addr)
                if val is not None:
                    return val
            except Exception:
                continue
    except Exception:
        pass
    return None


def _resolve_cell(
    ws,
    cached_ws,
    row: int,
    col: int,
    document: Document,
    warned: Set[str],
    formulas_model=None,
) -> str:
    """Resolve a single cell: prefer calculated value, then pure-Python, then formula text."""
    cell = ws.cell(row, col)
    value = cell.value

    if not (isinstance(value, str) and value.startswith("=")):
        return _display(value)

    # 1) Cached / LibreOffice-calculated value
    cached_value = cached_ws.cell(row, col).value
    if cached_value is not None and not (isinstance(cached_value, str) and cached_value.startswith("=")):
        return _display(cached_value)

    # 2) Pure-Python xlcalculator evaluator
    if formulas_model is not None:
        py_val = _cell_value_from_evaluator(formulas_model, ws.title, row, col)
        if py_val is not None and not (isinstance(py_val, str) and str(py_val).startswith("=")):
            return _display(py_val)

    # 3) Keep formula text + warn once
    coordinate = f"{ws.title}!{cell.coordinate}"
    if coordinate not in warned:
        document.warn(
            f"{coordinate}: formula has no calculated result; formula text was retained."
        )
        warned.add(coordinate)
    return value


def _region_rows(
    ws,
    cached_ws,
    top: int,
    bottom: int,
    left: int,
    right: int,
    merged: Dict[Tuple[int, int], object],
    document: Document,
    warned: Set[str],
    formulas_model=None,
) -> List[List[str]]:
    rows: List[List[str]] = []
    for row in range(top, bottom + 1):
        values = []
        for col in range(left, right + 1):
            value = ws.cell(row, col).value
            if value is None:
                value = merged.get((row, col))
            if value is not None and isinstance(value, str) and value.startswith("="):
                values.append(
                    _resolve_cell(ws, cached_ws, row, col, document, warned, formulas_model)
                )
            else:
                values.append(_display(value))
        if any(values):
            rows.append(values)
    return rows


def _header_index(rows: List[List[str]]) -> int:
    if len(rows) <= 1:
        return 0
    lookahead = min(4, len(rows))
    counts = [sum(bool(value.strip()) for value in row) for row in rows[:lookahead]]
    best = max(range(lookahead), key=lambda index: counts[index])
    return best if counts[best] >= 2 else 0


def convert_excel(path: Path, recalculate: bool = True) -> Document:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise RuntimeError("Excel support requires openpyxl. Install it with: pip install openpyxl") from exc

    if path.suffix.lower() == ".xls":
        raise RuntimeError(
            "Legacy .xls is not supported; convert it to .xlsx first (e.g. with LibreOffice)."
        )

    document = Document(
        str(path),
        "excel",
        path.stem,
        metadata={"formula_recalculation": "not_needed"},
    )
    warned: Set[str] = set()
    formulas_model = None

    with tempfile.TemporaryDirectory(prefix="document_md_excel_") as temp_name:
        temp_dir = Path(temp_name)

        probe = load_workbook(path, data_only=False, read_only=False)
        has_formulas = _workbook_has_formulas(probe)
        probe.close()

        calculation_source = path

        if has_formulas and recalculate:
            # Tier 1: LibreOffice
            lo_result = _recalculate_with_libreoffice(path, temp_dir, document)
            if lo_result is not None:
                calculation_source = lo_result
            else:
                # Tier 2: pure-Python xlcalculator library (MIT)
                formulas_model = _evaluate_with_xlcalculator(path, document)
                if document.metadata.get("formula_recalculation") == "not_needed":
                    document.metadata["formula_recalculation"] = "cached_only"
                    document.warn(
                        "No external calculator available (LibreOffice / xlcalculator). "
                        "Using cached Excel values only; cells without cache keep formula text."
                    )

        workbook = load_workbook(calculation_source, data_only=False, read_only=False)
        cached = load_workbook(calculation_source, data_only=True, read_only=False)
        document.metadata["sheets"] = workbook.sheetnames
        document.metadata["has_formulas"] = has_formulas

        try:
            for ws in workbook.worksheets:
                cached_ws = cached[ws.title]
                document.add(
                    Block("heading", ws.title, source={"sheet": ws.title}, metadata={"level": 2})
                )
                bounds = _used_bounds(ws)
                if bounds is None:
                    document.add(Block("paragraph", "(empty sheet)", source={"sheet": ws.title}))
                    continue

                top, bottom, left, right = bounds
                merged = _merged_values(ws)

                for band_top, band_bottom in _split_row_bands(ws, top, bottom, left, right, merged):
                    for region_left, region_right in _split_column_ranges(
                        ws, band_top, band_bottom, left, right, merged
                    ):
                        rows = _region_rows(
                            ws,
                            cached_ws,
                            band_top,
                            band_bottom,
                            region_left,
                            region_right,
                            merged,
                            document,
                            warned,
                            formulas_model=formulas_model,
                        )
                        if not rows:
                            continue
                        header = _header_index(rows)
                        for title_row in rows[:header]:
                            title = " ".join(value for value in title_row if value).strip()
                            if title:
                                document.add(
                                    Block(
                                        "paragraph",
                                        title,
                                        source={"sheet": ws.title, "row": band_top},
                                    )
                                )
                        table_rows = rows[header:]
                        source = {
                            "sheet": ws.title,
                            "rows": f"{band_top}:{band_bottom}",
                            "columns": f"{region_left}:{region_right}",
                        }
                        if len(table_rows) == 1:
                            document.add(
                                Block("paragraph", " | ".join(table_rows[0]), source=source)
                            )
                        else:
                            document.add(
                                Block(
                                    "table",
                                    rows=table_rows,
                                    source=source,
                                    confidence=0.82,
                                    metadata={
                                        "merged_cells": len(ws.merged_cells.ranges),
                                        "formula_values": has_formulas,
                                    },
                                )
                            )
        finally:
            workbook.close()
            cached.close()

    return document
