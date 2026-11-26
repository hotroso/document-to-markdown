"""Free-form PDF, image and Excel to Markdown converter with PP-Structure / PaddleOCR."""

__version__ = "0.3.2"

from .pipeline import convert_file

__all__ = ["convert_file", "__version__"]
