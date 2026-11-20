"""Mermaid null-barrier regressions run without the optional engine (gr548066)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from precis_pathway.text_views import graph_to_mermaid


@pytest.mark.parametrize(
    "estimate",
    [
        {"barrier": None, "barrier_std": None, "low_confidence": True},
        {"barrier": None, "barrier_std": 0.0, "low_confidence": False},
        {},
    ],
)
def test_mermaid_missing_barrier_is_unavailable(estimate: dict[str, Any]) -> None:
    graph = {
        "nodes": [{"id": "a"}, {"id": "b"}],
        "links": [{"source": "a", "target": "b", **estimate}],
    }

    rendered = graph_to_mermaid(graph)

    assert 'n_a -->|"Ea n/a' in rendered
    assert "\u26a0" in rendered
    assert "Ea 0.00" not in rendered


@pytest.mark.parametrize(
    ("estimate", "label"),
    [
        ({"barrier": 0.4, "barrier_std": 0.05}, "Ea 0.40\u00b10.05"),
        ({"barrier": 0.0, "barrier_std": 0.0}, "Ea 0.00\u00b10.00"),
        ({"barrier": 0.4, "barrier_std": None}, "Ea 0.40 (std n/a)"),
    ],
)
def test_mermaid_measured_barrier_keeps_its_value(
    estimate: dict[str, Any], label: str
) -> None:
    graph = {"links": [{"source": "a", "target": "b", **estimate}]}

    assert graph_to_mermaid(graph) == f'flowchart LR\n  n_a -->|"{label}"| n_b'


def test_mermaid_supply_link_needs_no_barrier() -> None:
    graph = {
        "links": [{"source": "a", "target": "b", "kind": "supply", "barrier": None}]
    }

    assert graph_to_mermaid(graph) == "flowchart LR\n  n_a -.->|supply| n_b"


def test_mermaid_hide_supply_drops_supply_edges() -> None:
    from precis_pathway.handler import PathwayHandler

    graph = {
        "nodes": [{"id": s} for s in ("a", "b", "side")],
        "links": [
            {"source": "a", "target": "b", "barrier": 0.4},
            {"source": "a", "target": "side", "kind": "supply"},
        ],
    }
    handler = object.__new__(PathwayHandler)
    ref = SimpleNamespace(id=42, slug="path", title="Pathway", meta={"graph": graph})
    setattr(
        handler,
        "hub",
        SimpleNamespace(live_store=SimpleNamespace(get_ref=lambda **_kw: ref)),
    )
    full = handler.get(id="path", view="mermaid").body
    pruned = handler.get(id="path", view="mermaid", args={"hide_supply": True}).body
    assert "supply" in full and "supply" not in pruned
    assert "side" not in pruned
    assert len(pruned.splitlines()) < len(full.splitlines())
