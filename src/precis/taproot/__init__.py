"""Taproot — the evidence-grounded claim graph.

Design: ``docs/proposals/taproot.md``. Phase 1 (this package's current
content) is the flat claim canonicalizer — the gate everything else
waits on. See ``docs/proposals/taproot-phase1-canonicalization.md`` for
the build ticket and :mod:`precis.taproot.canon` for the four functions.
"""

from __future__ import annotations
