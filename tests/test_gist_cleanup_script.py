"""scripts/gist-cleanup selection logic — pure function over (id, text) rows."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "_gist_cleanup.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_gist_cleanup", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_reject_rows_selects_leaks_and_keeps_clean() -> None:
    """Rows are ``(chunk_id, gist, chunk_text, cited)`` — the write gate's
    full inputs: scratchpad and verbatim-prefix checks need the chunk text,
    the identifier check needs the cited ``kind:value`` set."""
    mod = _load()
    prose = "We synthesized a copper catalyst and tested it for CO2 reduction."
    rows = [
        (
            1,
            "Cu catalysts raise CO2 reduction selectivity.\n\nDetail sentence.",
            prose,
            "",
        ),
        (2, "BRIEF: Cu catalysts raise selectivity\n\nDETAIL: more", prose, ""),
        (3, "x" * 900, prose, ""),
        (4, "", prose, ""),
        (5, "Keep brief, at most 15 words, so I will write: foo", prose, ""),
        (6, "per arXiv:2301.12345", prose, ""),
        (7, "per arXiv:2301.12345", prose, "arxiv:2301.12345"),
        (8, "adds specifics not in BRIEF: ratios. Let me draft:", prose, ""),
    ]
    got = mod.reject_rows(rows)
    ids = {c for c, _, _ in got}
    assert {1, 4, 7}.isdisjoint(ids)
    assert {2, 3, 5, 6, 8} <= ids
    assert all(isinstance(r, str) and r for _, r, _ in got)


def test_reason_class_drops_needle_and_counts() -> None:
    mod = _load()
    assert mod.reason_class("prompt-fragment echo 'keep brief'") == (
        "prompt-fragment echo"
    )
    assert mod.reason_class("summary over 800 chars (900)") == "summary over 800 chars"
