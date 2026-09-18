"""Ratchet on ``# type: ignore`` — the count may only go down (gr343722).

pyproject records a completed error-code burn-down (2026-08-02) but nothing
kept the per-site ignores from creeping back: 357 on 2026-09-16, 373 two
days later. A ceiling per tree makes growth visible in review — a diff that
needs a new ignore must lower one elsewhere or raise the number here on
purpose, in the same commit, where a reviewer sees it.

When you remove ignores, lower the ceiling to the new count so the slack
doesn't accumulate.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_IGNORE = re.compile(r"#\s*type:\s*ignore\b")

# Ceilings = the counts at the ratchet's introduction. Only ever lower them.
CEILINGS = {"src": 148, "tests": 225}


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
