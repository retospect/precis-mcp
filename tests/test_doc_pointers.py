"""Dead-pointer guard for the orientation docs.

The acquaintance path (README + AGENTS + CLAUDE + the docs maps) is a set
of hand-maintained indexes that drift silently — a fresh agent following a
stale link burns tokens chasing a file that moved or was deleted. This test
pins the *live* orientation docs so a dead relative link fails the gate.

It deliberately checks only markdown **link targets** (`[text](path)`), not
prose file mentions, and only the live orientation set — not `docs/design/`
or `docs/decisions/`, which are frozen historical artefacts that may point
at since-removed files on purpose. Hermetic: no DB, no model.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# Repo root: this file is <root>/tests/test_doc_pointers.py
ROOT = Path(__file__).resolve().parent.parent

# The live acquaintance path — the docs a fresh agent actually reads to
# orient. Add new top-level orientation docs here as they appear.
LIVE_DOCS = [
    "README.md",
    "AGENTS.md",
    "CLAUDE.md",
    "OPEN-ITEMS.md",
    "docs/architecture.md",
    "docs/README.md",
]

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

    dead: list[str] = []
    for target in _local_targets(doc_path.read_text(encoding="utf-8")):
        if not (doc_path.parent / target).resolve().exists():
            dead.append(target)

    assert not dead, f"{doc} has dead relative link(s): {sorted(set(dead))}"
