"""precis_pathway trust view: ``get(kind='pathway', id='<slug>',
view='trust')`` — catpath's structured per-step trust records
(``trust_schema == 1``, catpath ``docs/backlog/per-step-trust-records.md``)
rendered as a TOON table, record ids verbatim. See
``docs/handoff/precis-trust-consumer.md`` (catpath repo) for the contract.

Store-seeded fake ``meta['results']`` (no calculator, no NEB) — same
rationale as ``test_pathway_kinetics.py``: split out to stay cheap to target
on its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("autocatpath")

import precis_pathway
from precis.dispatch import Hub
from precis.store import Store
from precis_pathway.handler import PathwayHandler

_MIGRATIONS_DIR = Path(precis_pathway.__file__).parent / "migrations"

# Shaped like the catpath acceptance fixture (test_trust_records.py::
# test_offroute_competitor_blocks_selectivity_not_barrier): a clean on-route
# edge (R->M) plus an off-route fork competitor (M->S) whose detachment
# withholds selectivity without touching the barrier verdict.
_GRAPH: dict[str, Any] = {
    "directed": True,
    "nodes": [{"id": "R"}, {"id": "M"}, {"id": "P"}, {"id": "S"}],
    "links": [
        {
            "source": "R",
            "target": "M",
            "barrier": 0.5,
            "delta_e": -0.1,
            "low_confidence": False,
            "kind": "reaction",
        },
        {
            "source": "M",
            "target": "P",
            "barrier": 0.3,
            "delta_e": -0.2,
            "low_confidence": False,
            "kind": "reaction",
        },
    ],
}
_TRUST_RECORDS = [
    {
        "step": "R->M",
        "seed": 0,
        "check": "neb_convergence",
        "verdict": "pass",
        "severity": "fatal",
        "evidence": {"ts_image": 3},
        "id": "R->M#s0#neb_convergence",
    },
    {
        "state": "M",
        "step": "R->M",
        "seed": 0,
        "check": "relax_convergence",
        "verdict": "marginal",
        "severity": "warn",
        "evidence": {"fmax": 0.048, "steps": 71},
        "id": "M@R->M#s0#relax_convergence",
    },
    {
        "step": "M->S",
        "seed": 0,
        "check": "detachment",
        "verdict": "fail",
        "severity": "fatal",
        "evidence": {"image": 3, "closest_A": 4.81},
        "id": "M->S#s0#detachment",
    },
]
_RESULTS_V1 = {
    "pathway": ["R", "M", "P"],
    "target": "P",
    "trust_schema": 1,
    "trust": _TRUST_RECORDS,
    "route_steps": ["R->M", "M->P"],
    "trust_summary": {
        "barrier": {"available": True, "blocked_by": []},
        "selectivity": {
            "available": False,
            "blocked_by": [
                {
                    "fork": "M",
                    "competitor": "S",
                    "on_route": False,
                    "reasons": ["M->S#s0#detachment"],
                },
            ],
        },
    },
    "P_side": None,
    "P_side_blockers": [
        {"fork": "M", "competitor": "S", "reasons": ["M->S#s0#detachment"]},
    ],
}
_RESULTS_LEGACY = {"pathway": ["R", "M", "P"], "target": "P"}


@pytest.fixture
def pathway_store(store: Store, monkeypatch: pytest.MonkeyPatch) -> Store:
    """Mirrors ``test_pathway_kinetics.py``'s fixture of the same name
    (duplicated on purpose, keeps this file independently collectable)."""
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


def test_trust_view_renders_records_grouped_with_verbatim_ids(
    pathway_store: Store,
) -> None:
    h = _handler(pathway_store)
    _seed(
        pathway_store,
        "trust-rows",
        {"status": "ready", "graph": _GRAPH, "results": _RESULTS_V1},
    )

    r = h.get(id="trust-rows", view="trust")
    for rec in _TRUST_RECORDS:
        assert str(rec["id"]) in r.body  # ids are citable handles — never rewritten
    assert "detachment" in r.body
    assert "neb_convergence" in r.body
    assert "relax_convergence" in r.body
    assert "marginal" in r.body
    assert "fail" in r.body
    assert "pass" in r.body


def test_trust_view_no_trust_schema_gets_guidance_not_empty_table(
    pathway_store: Store,
) -> None:
    """A pathway harvested before the trust-records contract existed (its
    results carry no ``trust_schema``) must get a guidance message, not an
    empty or silently-misleading table."""
    h = _handler(pathway_store)
    _seed(
        pathway_store,
        "trust-legacy",
        {"status": "ready", "graph": _GRAPH, "results": _RESULTS_LEGACY},
    )

    r = h.get(id="trust-legacy", view="trust")
    assert "predates" in r.body
    assert "trust_schema" in r.body
    for rec in _TRUST_RECORDS:
        assert str(rec["id"]) not in r.body


def test_trust_view_no_graph_errors_cleanly(pathway_store: Store) -> None:
    """No computed graph → the same not-computed-yet guard as the other
    views, not a crash."""
    h = _handler(pathway_store)
    _seed(pathway_store, "trust-nograph", {"status": "preview"})

    r = h.get(id="trust-nograph", view="trust")
    assert "not computed yet" in r.body
