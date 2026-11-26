# Contributing to document-to-markdown

Thank you for your interest in contributing to **document-to-markdown**!

## Development Setup

```bash
git clone https://github.com/hotroso/document-to-markdown.git
cd document-to-markdown
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

To test OCR features locally, install the `[ocr]` or `[all]` extra:

```bash
pip install -e ".[ocr,dev]"
```

## Running Tests

```bash
pytest -v
```

Please make sure all existing tests pass and add unit tests for new behavior or bug fixes (prefer mocking heavy OCR engines in unit tests so CI remains fast and deterministic across platforms).

## Pull Request Guidelines

1. Fork the repository and create a feature/fix branch from `main`.
2. Keep changes focused and well-tested.
3. Follow [Conventional Commits](https://www.conventionalcommits.org/) for commit messages (`feat:`, `fix:`, `docs:`, `chore:`, `test:`).
4. Update `README.md` or `USAGE.md` if you add or change CLI flags or Python API options.
5. Open a Pull Request describing the motivation, changes, and how you tested them.

## Reporting Bugs

When opening a bug report, please include:
- OS and Python version (`python --version`)
- Installed `document-to-markdown`, `paddleocr`, and `paddlepaddle` versions
- The exact command run and full traceback / output
- Whether `--ocr-profile mobile` or `--no-structure` reproduces the issue
