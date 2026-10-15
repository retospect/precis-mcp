"""STEER slice 1: kept alternative routes + LLM strategy rerank.

No model is called: every test injects a fake ``ask``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis_chem import steer
from precis_chem.aizynth import parse_aizynth_trees
from precis_chem.ir import RouteGraph, RouteStep
from precis_chem.persist import apply_route_result
from precis_chem.route import RouteHandler
from tests.test_route_plugin import route_store  # noqa: F401 — fixture


def _tree(product: str, reactants: list[str], score: float) -> dict[str, Any]:
    return {
        "type": "mol",
        "smiles": product,
        "in_stock": False,
        "scores": {"state score": score},
        "children": [
            {
                "type": "reaction",
                "smiles": ".".join(reactants) + ">>" + product,
                "metadata": {"template": "t", "policy_probability": 0.5},
                "children": [
                    {"type": "mol", "smiles": r, "in_stock": True, "children": []}
                    for r in reactants
                ],
            }
        ],
    }


_TREES = [
    _tree("CC(=O)NC", ["CC(=O)O", "CN"], 0.9),
    _tree("CC(=O)NC", ["CC(=O)Cl", "CN"], 0.8),
    _tree("CC(=O)NC", ["CC(=O)OC(C)=O", "CN"], 0.7),
]


def _graph() -> RouteGraph:
    return parse_aizynth_trees(json.dumps(_TREES), target="CC(=O)NC")


def test_aizynth_parser_keeps_alternatives_and_roundtrips() -> None:
    g = _graph()
    assert g.steps[0].reactants == ["CC(=O)O", "CN"]
    assert [a.steps[0].reactants[0] for a in g.alternatives] == [
        "CC(=O)Cl",
        "CC(=O)OC(C)=O",
    ]
    back = RouteGraph.from_json(g.to_json())
    assert len(back.alternatives) == 2
    assert back.alternatives[0].alternatives == []
    assert "2 lower-ranked alternative route(s) kept" in g.render()
    only_top = parse_aizynth_trees(json.dumps(_TREES), max_alternatives=0)
    assert only_top.alternatives == []


def test_old_blob_without_alternatives_still_parses() -> None:
    blob = _graph().to_json()
    blob.pop("alternatives")
    assert RouteGraph.from_json(blob).alternatives == []


def test_parse_reply() -> None:
    assert steer.parse_reply("SCORE: 7\nRATIONALE: late amide.") == (7.0, "late amide.")
    assert steer.parse_reply("score: 14 rationale: x")[0] == 10.0
    assert steer.parse_reply("no idea")[0] is None


def test_strategy_validation() -> None:
    with pytest.raises(ValueError):
        steer.normalise_strategy("   ")
    with pytest.raises(ValueError):
        steer.normalise_strategy("x" * 600)
    assert steer.normalise_strategy(" avoid  acid chlorides ") == "avoid acid chlorides"


def _fake_ask(calls: list[str]) -> steer.Ask:
    """Scores the acid-chloride route low, fails on the anhydride route."""

    def ask(prompt: str) -> tuple[str, str | None]:
        calls.append(prompt)
        if "CC(=O)OC(C)=O" in prompt:
            return "", "model unavailable"
        if "CC(=O)Cl" in prompt:
            return "SCORE: 1\nRATIONALE: uses an acid chloride in step 1.", None
        return "SCORE: 9\nRATIONALE: direct coupling, no acid chloride.", None

    return ask


def test_steer_ranks_and_marks_unscored_last() -> None:
    calls: list[str] = []
    scores = steer.steer("avoid acid chlorides", _graph(), ask=_fake_ask(calls))
    assert len(calls) == 3
    assert "avoid acid chlorides" in calls[0]
    assert [(s.engine_rank, s.score) for s in scores] == [(1, 9.0), (2, 1.0), (3, None)]
    assert scores[2].error == "model unavailable"


def test_route_text_carries_step_classes() -> None:
    g = RouteGraph(
        target="T",
        engine="stub",
        engine_version="0",
        steps=[
            RouteStep(
                id=1,
                product="T",
                reactants=["A", "B"],
                reaction_class="RXNO:0000357",
                class_name="N-acylation to amide",
                in_stock=True,
            )
        ],
    )
    assert "step 1: T <= A + B [N-acylation to amide] (in stock)" in steer.route_text(g)


def test_view_steer_scores_caches_and_refreshes(
    route_store: Store,  # noqa: F811
) -> None:
    h = RouteHandler(hub=Hub(store=route_store))
    h.put(id="amide", target="CC(=O)NC", engine="stub")
    ref = route_store.get_ref(kind="route", id="amide")
    assert ref is not None
    apply_route_result(route_store, ref.id, _graph(), cache_key="k")
    calls: list[str] = []
    h.steer_ask = _fake_ask(calls)

    body = h.get(id="amide", view="steer", q="avoid acid chlorides").body
    assert "1. engine route #1: score 9" in body
    assert "2. engine route #2: score 1" in body
    assert "UNSCORED (model unavailable)" in body
    assert len(calls) == 3

    again = h.get(id="amide", view="steer", q="avoid  acid chlorides").body
    assert "cached" in again and len(calls) == 3

    h.get(id="amide", view="steer", q="avoid acid chlorides", refresh=True)
    assert len(calls) == 6

    with pytest.raises(BadInput):
        h.get(id="amide", view="steer")
