"""precis_pathway kinetics view: ``get(kind='pathway', id='<slug>',
view='kinetics')`` — Eyring rates + residence times + rate-limiting step over
a computed graph (Simulation step deep-links,
docs/backlog/quest-dossier-dialectic.md). Honest v1 only: no steady-state
coverages, no degree-of-rate-control.

Store-seeded fake ``meta['graph']`` (no calculator, no NEB) — split out of
``tests/test_pathway_plugin.py`` on purpose, same rationale as
``test_pathway_step_selector.py``: that file's 81 real-EMT simulation tests
(``pytest.mark.slow``) make it expensive to target on its own for a quick
view check. Still needs ``autocatpath`` importable (``PathwayHandler.__init__``
imports ``precis_pathway.runner``), so it skips cleanly on a host without it.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("autocatpath")

import precis_pathway
from precis.dispatch import Hub
from precis.store import Store
from precis_pathway.handler import PathwayHandler

_MIGRATIONS_DIR = Path(precis_pathway.__file__).parent / "migrations"

# A 3-edge chain A→B→C→D — networkx node_link_data shape (edges='links'), same
# as test_pathway_step_selector.py. meta['results']['pathway'] gives
# analysis.roots() an order to resolve root/target from, so
# analysis.reaction_path() actually walks the chain.
_GRAPH: dict[str, Any] = {
    "directed": True,
    "nodes": [{"id": "A"}, {"id": "B"}, {"id": "C"}, {"id": "D"}],
    "links": [
        {
            "source": "A",
            "target": "B",
            "barrier": 0.5,
            "barrier_std": 0.02,
            "delta_e": -0.1,
            "low_confidence": False,
            "kind": "reaction",
        },
        {
            "source": "B",
            "target": "C",
            "barrier": 0.74,
            "barrier_std": 0.05,
            "delta_e": -0.32,
            "low_confidence": False,
            "kind": "reaction",
        },
        {
            "source": "C",
            "target": "D",
            "barrier": 1.2,
            "barrier_std": 0.1,
            "delta_e": 0.4,
            "low_confidence": True,
            "kind": "reaction",
        },
    ],
}
_RESULTS = {"pathway": ["A", "B", "C", "D"], "target": "D"}


def _eyring(ea_ev: float, t_k: float = 300.0) -> float:
    kb = 8.617e-5  # eV/K
    h = 4.136e-15  # eV*s
    kt = kb * t_k
    return (kt / h) * math.exp(-ea_ev / kt)


@pytest.fixture
def pathway_store(store: Store, monkeypatch: pytest.MonkeyPatch) -> Store:
    """The shared test store with the precis_pathway migration seeded + the
    dark flag on (mirrors test_pathway_step_selector.py's fixture of the same
    name — duplicated rather than imported to keep this file independent and
    cheap to collect on its own)."""
    monkeypatch.setenv("PRECIS_AUTOCATPATH_ENABLED", "1")
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)
    return store


def _handler(store: Store) -> PathwayHandler:
    hub = Hub(store=store)
    h = PathwayHandler(hub=hub)
    h._register_with(hub)
    return h


def _seed(store: Store, slug: str, meta: dict[str, Any]) -> int:
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="pathway", slug=slug, title=f"{slug} pathway", meta=meta, conn=conn
        )
    return ref.id


def test_kinetics_view_one_row_per_edge(pathway_store: Store) -> None:
    h = _handler(pathway_store)
    _seed(
        pathway_store,
        "kin-rows",
        {"status": "ready", "graph": _GRAPH, "results": _RESULTS},
    )

    r = h.get(id="kin-rows", view="kinetics")
    assert "A→B" in r.body
    assert "B→C" in r.body
    assert "C→D" in r.body


def test_kinetics_view_eyring_rate_spot_check(pathway_store: Store) -> None:
    h = _handler(pathway_store)
    _seed(
        pathway_store,
        "kin-rate",
        {"status": "ready", "graph": _GRAPH, "results": _RESULTS},
    )

    r = h.get(id="kin-rate", view="kinetics")
    expected_k = _eyring(0.74)  # the B→C step's barrier, at the fixed T=300K
    # rendered at 2 sig figs (e.g. "2.29e+00") — match the mantissa+exponent.
    assert f"{expected_k:.2e}" in r.body


def test_kinetics_view_rate_limiting_is_max_barrier(pathway_store: Store) -> None:
    h = _handler(pathway_store)
    _seed(
        pathway_store,
        "kin-rls",
        {"status": "ready", "graph": _GRAPH, "results": _RESULTS},
    )

    r = h.get(id="kin-rls", view="kinetics")
    assert "Rate-limiting step: C→D" in r.body
    assert "1.20" in r.body  # C→D's Ea


def test_kinetics_view_flags_low_confidence_edge(pathway_store: Store) -> None:
    h = _handler(pathway_store)
    _seed(
        pathway_store,
        "kin-lowconf",
        {"status": "ready", "graph": _GRAPH, "results": _RESULTS},
    )

    r = h.get(id="kin-lowconf", view="kinetics")
    assert "low" in r.body  # C→D is low_confidence in the fixture


def test_kinetics_view_has_caveat_line(pathway_store: Store) -> None:
    h = _handler(pathway_store)
    _seed(
        pathway_store,
        "kin-caveat",
        {"status": "ready", "graph": _GRAPH, "results": _RESULTS},
    )

    r = h.get(id="kin-caveat", view="kinetics")
    assert "electronic" in r.body
    assert "NEB" in r.body
    assert "ZPE" in r.body
    assert "Eyring" in r.body


def test_kinetics_view_no_graph_errors_cleanly(pathway_store: Store) -> None:
    """No computed graph → a clean guidance message (mirrors the 'analysis'/
    'steps'/'warnings' views' not-computed-yet guard), not a crash."""
    h = _handler(pathway_store)
    _seed(pathway_store, "kin-nograph", {"status": "preview"})

    r = h.get(id="kin-nograph", view="kinetics")
    assert "not computed yet" in r.body
