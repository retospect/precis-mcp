"""``precis_ws`` — the workspace script helper, staged inside the sandbox.

``from precis_ws import out, params`` — ``params()`` reads
``/work/params.json``; ``out`` collects the run's manifest
(``out.block``/``out.instance``/``out.connect``) and writes
``out/manifest.json`` + ``out/trace.json`` at :func:`out.finish` (or
interpreter exit). Importing this package activates
:mod:`precis_geom.trace` when a work dir exists, so every ``@traced``
primitive the script calls lands in the trace — nothing else required.

Stdlib + the ``precis-geom`` wheel only; the script never writes the DB —
the importer is the trust boundary.
"""

from __future__ import annotations

import os
from pathlib import Path

from precis_geom import trace as _trace

_WORK_ROOT = Path(os.environ.get("PRECIS_WS_WORK_DIR") or "/work")
if _WORK_ROOT.is_dir():
    _trace.start(work_root=_WORK_ROOT)

from precis_ws import out, params

__all__ = ["out", "params"]
