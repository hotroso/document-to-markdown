"""Local PDF, Office (DOCX, PPTX, Excel), Email, HTML, CSV, and image to Markdown converter."""

__version__ = "0.6.1"

from .pipeline import convert_file

__all__ = ["convert_file", "__version__"]
