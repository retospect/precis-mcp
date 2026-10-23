"""``params()`` — the run's parameters, ``/work/params.json``.

The executor stages the params the ``run`` call was given as
``/work/params.json`` (slice 1); a missing file gives ``{}`` so scripts
stay runnable bare (``python main.py`` in a checkout)."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any


@lru_cache(maxsize=1)
def params() -> dict[str, Any]:
    root = Path(os.environ.get("PRECIS_WS_WORK_DIR") or "/work")
    path = root / "params.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return dict(raw) if isinstance(raw, dict) else {}


__all__ = ["params"]
