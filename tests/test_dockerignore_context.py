"""The image build context leaves out the per-checkout trees (gr696402).

``docker/Dockerfile`` builds from the repo root and its ``dev`` stage runs
``COPY . /app``, so everything ``.dockerignore`` does not name is sent to the
builder. In a checkout that hosts worktrees, ``.claude/worktrees/`` holds one
full copy of the repo per session: ``scripts/build-image precis-dev`` run from
such a checkout sent over 300 GB of context and was killed after 40 minutes.
``scratch/`` is each tree's dev-loop scratch dir, unbounded in the same way.

No build stage needs either one. Tests and gates read them through the
worktree bind mount (``precis-dev``) or the ``git archive`` sync
(``precis-gate``), never from the baked ``/app``.
"""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _rules() -> list[str]:
    """``.dockerignore`` patterns in file order, comments and blanks dropped,
    without the leading and trailing ``/`` Docker ignores."""
    lines = (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
    return [
        rule.strip("/") if not rule.startswith("!") else "!" + rule[1:].strip("/")
        for rule in (line.strip() for line in lines)
        if rule and not rule.startswith("#")
    ]


@pytest.mark.parametrize("name", [".claude", "scratch"])
def test_build_context_excludes_per_checkout_dir(name: str) -> None:
    rules = _rules()
    assert name in rules, (
        f".dockerignore has no `{name}/` entry, so the image build context "
        f"carries all of {name}/"
    )
    # Docker applies the last matching rule, so a later `!` rule under the
    # directory would send part of it after all.
    readded = [
        rule
        for rule in rules
        if rule.startswith("!") and fnmatch(name, rule[1:].split("/")[0])
    ]
    assert not readded, f".dockerignore re-includes part of {name}/: {readded}"
