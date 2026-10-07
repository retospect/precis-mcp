"""SE stays first-class in the MCP roster, skills and direct/cross-kind queries."""

from __future__ import annotations

import json

from precis.server import _kinds_loaded_line


def test_se_in_server_overview_and_toc(runtime_with_store):
    runtime = runtime_with_store
    assert "se" in runtime.hub.kinds
    assert "se" in _kinds_loaded_line(runtime).removeprefix("Kinds: ").rstrip(
        "."
    ).split(", ")
    overview = runtime.dispatch("get", {"kind": "skill", "id": "precis-overview"})
    assert "| `se` |" in overview
    toc = runtime.dispatch("get", {"kind": "skill", "id": "toc", "kinds": "se"})
    assert "precis-se-help" in toc


def test_se_direct_and_cross_kind_search_are_registered(runtime_with_store):
    runtime = runtime_with_store
    slug = "r14-kind-search-probe"
    text = json.dumps({"description": "r14kindvisibilityprobe", "ops": []})
    created = runtime.dispatch("put", {"kind": "se", "id": slug, "text": text})
    assert "[error:" not in created
    for kind in ("se", "se,memory"):
        result = runtime.dispatch(
            "search", {"kind": kind, "q": "r14kindvisibilityprobe"}
        )
        assert "[error:" not in result
        assert slug in result
