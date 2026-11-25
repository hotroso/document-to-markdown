"""Command-line entry point for the free-form document converter."""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from pathlib import Path
from typing import Iterable, List

from . import __version__
from .image_adapter import IMAGE_SUFFIXES
from .pipeline import convert_file

SUPPORTED = {".pdf", ".xlsx", ".xlsm"} | IMAGE_SUFFIXES

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def _inputs(values: Iterable[str]) -> List[Path]:
    result: List[Path] = []
    for value in values:
        path = Path(value)
        if path.is_dir():
            result.extend(
                sorted(
                    item
                    for item in path.rglob("*")
                    if item.is_file() and item.suffix.lower() in SUPPORTED
                )
            )
        elif path.is_file():
            result.append(path)
        else:
            print(f"[skip] Input not found: {path}", file=sys.stderr)
    return list(dict.fromkeys(result))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert free-form PDF, images and Excel files to AI-friendly Markdown."
    )
    parser.add_argument("inputs", nargs="+", help="Files or directories to convert")
    parser.add_argument("-o", "--output", help="Output .md file or output directory")
    parser.add_argument(
        "--mode",
        choices=["balanced", "fidelity", "ai"],
        default="balanced",
        help="Markdown style: balanced (default), fidelity (more structure), ai (source comments)",
    )
    parser.add_argument(
        "--ocr",
        choices=["off", "auto", "paddle", "tesseract"],
        default="auto",
        help="OCR engine: auto (prefer PaddleOCR / PP-Structure), paddle, tesseract, or off",
    )
    parser.add_argument(
        "--lang",
        default="eng",
        help="OCR language (eng, vie, eng+vie, …). Mapped automatically for Paddle.",
    )
    parser.add_argument(
        "--no-structure",
        action="store_true",
        help="Disable PP-Structure layout/table analysis (use plain OCR only)",
    )
    parser.add_argument(
        "--assets",
        "--images",
        dest="export_images",
        action="store_true",
        default=False,
        help="Export images extracted from PDF into an _assets directory",
    )
    parser.add_argument(
        "--no-images",
        dest="export_images",
        action="store_false",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--meta",
        dest="export_meta",
        action="store_true",
        default=False,
        help="Export machine-readable .meta.json sidecar file",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Convert only the first three PDF pages",
    )
    parser.add_argument(
        "--password",
        action="store_true",
        help="Prompt for a PDF password when needed",
    )
    parser.add_argument(
        "--no-recalculate",
        action="store_true",
        help="Do not ask LibreOffice to recalculate Excel formulas",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    return parser


def main(argv: List[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    sources = _inputs(args.inputs)
    if not sources:
        print("No supported input files found.", file=sys.stderr)
        return 2

    output_arg = Path(args.output) if args.output else None
    multiple = len(sources) > 1 or any(Path(value).is_dir() for value in args.inputs)
    failures = 0
    for source in sources:
        if output_arg is None:
            target = source.with_suffix(".md")
        elif multiple or output_arg.is_dir() or output_arg.suffix.lower() != ".md":
            target = output_arg / (source.stem + ".md")
        else:
            target = output_arg
        password = (
            getpass.getpass(f"Password for {source.name}: ")
            if args.password and source.suffix.lower() == ".pdf"
            else None
        )
        try:
            result = convert_file(
                source,
                target,
                mode=args.mode,
                ocr=args.ocr,
                ocr_lang=args.lang,
                export_images=args.export_images,
                export_meta=args.export_meta,
                preview=args.preview,
                password=password,
                recalculate=not args.no_recalculate,
                use_structure=not args.no_structure,
            )
            print(f"[ok] {source} -> {result}")
        except Exception as exc:
            failures += 1
            print(f"[error] {source}: {exc}", file=sys.stderr)
            if args.verbose:
                logging.exception("Conversion failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
