#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""Shared bare-Python-identifier heuristic for coderef nudges.

Both ``coderef-nudge.py`` (matcher: Grep) and ``bash-reflex-nudge.py``
(matcher: Bash — greps run via the shell, e.g. ``rg``/``grep``/``egrep``) need
the exact same "is this search pattern a symbol lookup, not a text search?"
call. Kept here so the two hooks can't drift apart — a change to the
identifier/stop-word rule updates both nudges at once. The same applies to
``path_arg_is_pythonish``: the "is the search target clearly not Python?"
verdict must be identical whichever tool carried the search (gr514328).

Deliberately narrow: len >= 3, a bare ``[A-Za-z_][A-Za-z0-9_]*`` token, not in
the common-English/keyword ``STOP`` set. See the callers' own docstrings for
why this stays conservative (a nudge that fires too often gets tuned out).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Bare words that are almost never a symbol lookup worth a call-graph query —
# keeps the nudge from firing on common English / keyword greps.
STOP = {
    "test",
    "todo",
    "fixme",
    "note",
    "true",
    "false",
    "none",
    "null",
    "self",
    "cls",
    "def",
    "class",
    "import",
    "return",
    "async",
    "await",
    "the",
    "and",
    "for",
    "not",
    "with",
    "type",
    "data",
    "value",
    "error",
    "name",
    "path",
    "file",
    "line",
    "text",
    "main",
    "init",
}


def is_symbol_candidate(tok: str) -> bool:
    """True if ``tok`` reads as a bare Python identifier worth a coderef nudge."""
    tok = tok.strip()
    return len(tok) >= 3 and bool(IDENT.match(tok)) and tok.lower() not in STOP


def path_arg_is_pythonish(arg: str, base: str = "") -> bool | None:
    """Classify a grep/search TARGET argument (a path, not the pattern).

    ``True`` — Python: a ``.py``/``.pyi`` path, or an existing directory
    whose direct entries include a ``.py`` file. ``False`` — clearly
    non-Python: a path carrying another suffix (``x.sql``,
    ``migrations/*.sql``), or an existing directory whose entries are all
    regular files, none of them Python (``migrations/`` holds only
    ``*.sql``). ``None`` — can't tell (no suffix and missing, a dir with
    subdirectories or no files); callers keep their code-ish default so a
    nudge stays silent only on positive evidence.
    """
    p = Path(arg)
    suffix = p.suffix.lower()
    if suffix:
        return suffix in (".py", ".pyi")
    cand = p if p.is_absolute() else Path(base or os.getcwd()) / p
    try:
        if not cand.is_dir():
            return None
        has_py = has_subdir = has_file = False
        for e in cand.iterdir():
            if e.is_dir():
                has_subdir = True
            elif e.suffix.lower() in (".py", ".pyi"):
                has_py = True
            else:
                has_file = True
    except OSError:
        return None
    if has_py:
        return True
    if has_file and not has_subdir:
        return False
    return None
