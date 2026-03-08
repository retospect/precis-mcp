"""Document parsers — DOCX and LaTeX."""

from precis.parser.base import BaseParser
from precis.parser.docx import DocxParser
from precis.parser.latex import LatexParser


def get_parser(path_str: str) -> BaseParser:
    """Return the appropriate parser based on file extension."""
    if path_str.endswith(".docx"):
        return DocxParser()
    elif path_str.endswith(".tex"):
        return LatexParser()
    else:
        raise ValueError(f"Unsupported file format: {path_str}")
