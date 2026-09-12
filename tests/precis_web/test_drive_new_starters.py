"""``_NEW_STARTERS`` ("+ New" dropdown) — starter source must actually parse.

Each starter's ``put`` args are what the "+ New" button in Drive hands
straight to the real handler. A stale unitless cad starter is a live
functional break, not doc rot — creating a new cad design from Drive would
fail with ``UnitRequiredError`` on the very first click (units-cutover
prompt-surface audit, item 4).
"""

from __future__ import annotations

import json

from precis.cad.scene import parse_source
from precis_web.routes.drive import _NEW_STARTERS


def test_cad_starter_parses_under_boundary_grammar() -> None:
    kind, args, redirect = _NEW_STARTERS["cad"]("test-slug")
    assert kind == "cad"
    assert args["id"] == "test-slug"
    parse_source(args["text"])  # must not raise UnitRequiredError/DslError/SceneError
    assert redirect == "/cad/test-slug"


def test_structure_starter_is_valid_json() -> None:
    kind, args, redirect = _NEW_STARTERS["structure"]("test-slug")
    assert kind == "structure"
    payload = json.loads(args["text"])
    assert "cell" in payload and "ops" in payload
