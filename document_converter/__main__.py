"""Module entry point for `python -m document_converter` and standalone builds."""

from document_converter.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
