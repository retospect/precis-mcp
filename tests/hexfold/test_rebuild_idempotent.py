"""Rebuilding an already-built spec is idempotent.

``canonical_json(net)`` (what the precis se generator calls) and the
sectioned-JSON content hash both re-run ``build`` on a ``Net``'s spec, whose
menus are already expanded and whose 9-6/8-7 registrations are already
solved.  Before the fix, ``menus.expand`` re-expanded (duplicating neck
instances and connects — a KeyError for DA-neck) and the bud-attach solver
appended its six bond lines to the expansion record a second time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hexfold.build import build
from hexfold.canon import canonical_json, content_hash

_EXAMPLES = sorted(Path("hexfold/examples").glob("*.hx"))


@pytest.mark.parametrize("path", _EXAMPLES, ids=[p.stem for p in _EXAMPLES])
def test_canonical_json_of_net_equals_canonical_json_of_text(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    net = build(text)
    assert canonical_json(net) == canonical_json(text)
    assert content_hash(net) == content_hash(text)


def test_double_build_keeps_connect_count() -> None:
    text = Path("hexfold/examples/nanobud_87.hx").read_text(encoding="utf-8")
    once = build(text)
    twice = build(once.spec)
    assert len(twice.spec.connects) == len(once.spec.connects)
    assert len(twice.bonds) == len(once.bonds)
    menu = [c for c in twice.spec.connects if c.verb == "menu"][0]
    assert menu.expanded is not None
    assert len(menu.expanded["connects"]) == len(set(menu.expanded["connects"]))
