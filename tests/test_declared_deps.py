"""Modules imported by in-tree code must be declared, not ride a
transitive resolve. One test per accidental-dependency gripe; keep the
assertions cheap (tomllib, no resolve)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _dist_name(req: str) -> str:
    return re.split(r"[\s<>=!~;\[]", req.strip(), maxsplit=1)[0].lower()


def test_opencv_is_declared_not_transitive() -> None:
    """gr514225: ``cv2`` resolved only via surya-ocr's dep list — an
    ``import cv2`` would silently break if surya dropped it. Declared in
    ``[paper]`` (the extra the ingest/digitise paths install) so pyproject
    names it directly."""
    extras = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["optional-dependencies"]
    names = {_dist_name(req) for req in extras["paper"]}
    assert "opencv-python-headless" in names
