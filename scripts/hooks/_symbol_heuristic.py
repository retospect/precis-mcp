#!/usr/bin/env python3
"""Shared bare-Python-identifier heuristic for coderef nudges.

Both ``coderef-nudge.py`` (matcher: Grep) and ``bash-reflex-nudge.py``
(matcher: Bash — greps run via the shell, e.g. ``rg``/``grep``/``egrep``) need
the exact same "is this search pattern a symbol lookup, not a text search?"
call. Kept here so the two hooks can't drift apart — a change to the
identifier/stop-word rule updates both nudges at once.

Deliberately narrow: len >= 3, a bare ``[A-Za-z_][A-Za-z0-9_]*`` token, not in
the common-English/keyword ``STOP`` set. See the callers' own docstrings for
why this stays conservative (a nudge that fires too often gets tuned out).
"""

from __future__ import annotations

import re

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
