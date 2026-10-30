"""Compatibility entry for the generated ``examples/boards/nano`` workspace.

The former 32K implementation is replaced by the writer-generated board and
its pinned offline catalog. Its batch and check findings match this entry's
original fixture; keep the path for existing workspace callers.
"""

from __future__ import annotations

from pathlib import Path
from runpy import run_path

import precis_geom.pcb as pcb


def build() -> pcb.Board:
    path = Path(__file__).resolve().parents[1] / "nano" / "main.py"
    return run_path(str(path))["build"]()


if __name__ == "__main__":
    pcb.write_manifest(build(), "out")
