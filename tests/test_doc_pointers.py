"""Dead-pointer guard for the live docs.

The acquaintance path (README + AGENTS + CLAUDE + the docs maps, plus
every current-state doc reachable from it) is a set of hand-maintained
indexes that drift silently — a fresh agent following a stale link burns
tokens chasing a file that moved or was deleted. This test pins the *live*
docs so a dead relative link fails the gate.

It deliberately checks only markdown **link targets** (`[text](path)`), not
prose file mentions. Also excludes
`src/precis/data/skills/` — those docs use `[text](<placeholder>)` link
syntax to document call patterns, not real file links, so scanning them
produces false positives rather than catching real drift. Hermetic: no DB,
no model.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# Repo root: this file is <root>/tests/test_doc_pointers.py
ROOT = Path(__file__).resolve().parent.parent

_FROZEN_DIRS: tuple[str, ...] = ()
_FROZEN_EXEMPT: set[str] = set()


# Gitignored derived files (scripts/docs-index output): regenerated per
# worktree at session start, so a copy on disk can be transiently stale
# (e.g. mid-ship, after merging main) — link-checking it is checking a
# cache, not a doc.
_GENERATED = (
    "docs/backlog/INDEX.md",
    "docs/runbooks/INDEX.md",
    "docs/codebase-map.md",
)


def _live_docs() -> list[str]:
    """Every current-state markdown doc, minus frozen-historical dirs and
    gitignored generated indexes."""
    globs = [
        ROOT.glob("*.md"),
        ROOT.glob("docs/**/*.md"),
    ]
    out: set[str] = set()
    for g in globs:
        for path in g:
            rel = path.relative_to(ROOT).as_posix()
            if rel in _FROZEN_EXEMPT:
                out.add(rel)
                continue
            if rel.startswith(_FROZEN_DIRS) or rel in _GENERATED:
                continue
            out.add(rel)
    return sorted(out)


# The live acquaintance path — every current-state doc a fresh agent might
# read to orient.
LIVE_DOCS = _live_docs()

# [text](target) — capture the target.
_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _local_targets(text: str) -> list[str]:
    """Relative link targets worth resolving on disk.

    Drops external URLs and in-page anchors; strips a trailing #anchor and
    surrounding backticks/whitespace.
    """
    out: list[str] = []
    for raw in _LINK.findall(text):
        target = raw.strip().strip("`").strip()
        if not target or target.startswith("#"):
            continue
        if re.match(r"^[a-z][a-z0-9+.-]*:", target):  # http:, https:, mailto:
            continue
        target = target.split("#", 1)[0].strip()  # drop #anchor
        if target:
            out.append(target)
    return out


@pytest.mark.parametrize("doc", LIVE_DOCS)
def test_orientation_doc_links_resolve(doc: str) -> None:
    doc_path = ROOT / doc
    assert doc_path.exists(), f"orientation doc missing: {doc}"

    generated = {(ROOT / g).resolve() for g in _GENERATED}
    dead: list[str] = []
    for target in _local_targets(doc_path.read_text(encoding="utf-8")):
        resolved = (doc_path.parent / target).resolve()
        # Links TO a generated index are allowed to dangle: the target is
        # gitignored and regenerated per worktree (scripts/docs-index), so
        # it's legitimately absent right after a merge or in a fresh clone.
        if resolved in generated:
            continue
        if not resolved.exists():
            dead.append(target)

    assert not dead, f"{doc} has dead relative link(s): {sorted(set(dead))}"
