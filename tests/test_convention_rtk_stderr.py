"""Ratchet for rtk/no-blanket-stderr-suppression (docs/conventions/rtk.md).

``2>/dev/null`` discards stderr before rtk sees it, turning failures into
silence; noise belongs in ``.rtk/filters.toml``. Existing uses (many are
legitimate control flow, e.g. ``command -v foo || ...``) are a measured
baseline; the count may only go down. When it drops, lower ``_BASELINE``.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PATTERN = re.compile(r"2\s*>\s*/dev/null")
_BASELINE = 359  # measured 2026-10-17 over scripts/ + .claude/{agents,commands,skills,settings.json}

_SCAN = (
    _ROOT / "scripts",
    _ROOT / ".claude" / "agents",
    _ROOT / ".claude" / "commands",
    _ROOT / ".claude" / "skills",
    _ROOT / ".claude" / "settings.json",
)


def count_suppressions(text: str) -> int:
    return len(_PATTERN.findall(text))


def _files() -> list[Path]:
    out: list[Path] = []
    for base in _SCAN:
        if base.is_file():
            out.append(base)
        elif base.is_dir():
            out += [
                p
                for p in base.rglob("*")
                if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
            ]
    return sorted(out)


def measure() -> int:
    total = 0
    for p in _files():
        try:
            total += count_suppressions(p.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            continue
    return total


def test_counter_sees_synthetic_suppression() -> None:
    assert (
        count_suppressions("git foo 2>/dev/null || true\nx 2> /dev/null\nok 2>&1\n")
        == 2
    )
    assert count_suppressions("echo hi\n") == 0


def test_stderr_suppressions_do_not_grow() -> None:
    assert (_ROOT / "scripts").is_dir()
    assert _files(), "no files scanned: scan roots moved?"
    n = measure()
    assert n <= _BASELINE, (
        f"{n} `2>/dev/null` occurrences vs measured baseline {_BASELINE}: add an "
        ".rtk/filters.toml rule instead of hiding stderr (or use it only where "
        "silence is control flow and raise the baseline with a reason)"
    )
    assert n == _BASELINE, f"fixed some; lower _BASELINE to {n}"
