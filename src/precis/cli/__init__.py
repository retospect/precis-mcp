"""Precis CLI — ``precis serve | migrate | jobs ...``.

The CLI used to live in ``src/precis/cli.py``; it now lives as a
package so each subcommand implementation has its own module. This
top-level ``__init__`` re-exports the symbols external code
(console-script entry point, tests, help skills) still imports:

- :func:`main` — the console-script ``precis`` entry point.
- :func:`_build_parser` — argparse construction (used by CLI tests).
- :func:`_parse_interval` — ``--every`` spec parser (used by the
  patent-watch CLI tests).

Everything else lives in submodules under :mod:`precis.cli`; nothing
consumed externally by ``from precis.cli import X`` should break.
"""

from __future__ import annotations

from precis.cli.main import _build_parser, main
from precis.cli.patent import _parse_interval

__all__ = [
    "_build_parser",
    "_parse_interval",
    "main",
]
