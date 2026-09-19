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
CEILINGS = {"src": 156, "tests": 240}


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
