"""Ratchet on ``# type: ignore`` — the count may only go down (gr343722).

pyproject records a completed error-code burn-down (2026-08-02) but nothing
kept the per-site ignores from creeping back: 357 on 2026-09-16, 389 two
days later (153 src + 237 tests when this landed). A ceiling per tree makes
growth visible in review — a diff that needs a new ignore must lower one
elsewhere or raise the number here on purpose, in the same commit, where a
reviewer sees it.

The ceilings landed with +3 slack per tree: main gained an ignore on three
of the four CI runs that tried to ship this, and an exact ceiling can't
land under that churn. Lower them to the exact count at the next quiet
ship, and whenever you remove ignores, so the slack doesn't accumulate.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_IGNORE = re.compile(r"#\s*type:\s*ignore\b")

# Counts at introduction + 3 slack (see module docstring). Only ever lower.
# src 156 -> 158 on 2026-09-29, the deliberate reviewed raise this test's own
# failure message points at. Both new sites arrived with feature landings in a
# nine-session burst day (hexfold catalogue 7825a7ff, taxonomy-bootstrap
# e1a6f183) and left main red on the ratchet alone — every other check green.
# Staged rather than fixed: the packages carrying them are young and still
# moving, so typing the call sites now would be rewritten within the week.
#
# This is DEBT, not a new normal. The ratchet's job is to make growth cost a
# decision, and this raise is that decision being made once, not the ceiling
# becoming elastic. `src/precis/taxonomy/discovery.py`'s
# `int(ref_raw)  # type: ignore[call-overload]` is the cheap one to retire —
# `row.get()` returns `object`, so narrowing the row type kills it outright.
#
# tests 244 -> 247 the same day and for the same reason: the burst landed new
# suites (chain ops/DRC/origami, hexfold catalogue, the pcb/ewod and web
# additions) faster than their ignores were retired. Note the asymmetry worth
# watching — tests/ now carries 247 ignores against src/'s 158, so the suite is
# the heavier offender and is where a cleanup pass would pay best.
# 2026-10-01 src 156 -> 157: TaxonHandler.link adds meta= over the
# keyword-only NumericRefHandler.link, the same Handler.link(**kw) override
# every handler link carries. Then 157 -> 142 and tests 244 -> 240: shapely
# joined pyproject's mypy ignore_missing_imports overrides, retiring every
# per-import `import-untyped` ignore on it.
CEILINGS = {"src": 142, "tests": 240}


def _count(tree: str) -> Counter[str]:
    hits: Counter[str] = Counter()
    for path in sorted((REPO_ROOT / tree).rglob("*.py")):
        if path == Path(__file__).resolve():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if _IGNORE.search(line):
                hits[str(path.relative_to(REPO_ROOT))] += 1
    return hits


def test_type_ignore_count_never_grows() -> None:
    for tree, ceiling in CEILINGS.items():
        hits = _count(tree)
        total = sum(hits.values())
        worst = ", ".join(f"{p} ({n})" for p, n in hits.most_common(5))
        assert total <= ceiling, (
            f"{tree}/ carries {total} `type: ignore` sites, ceiling {ceiling} — "
            f"fix the type at the new site or remove an old ignore; raising "
            f"CEILINGS in {Path(__file__).name} is the deliberate, reviewed "
            f"escape. Heaviest files: {worst}"
        )
