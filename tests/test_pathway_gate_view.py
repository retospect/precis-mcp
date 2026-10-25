"""``get(kind='pathway', id='<slug>', view='trust')`` — the trust-gate
diagnostic at the handler seam (gr515871): the view is registered, an
uncomputed pathway gets the shared not-computed message, a pre-schema
artifact gets the trust-records refusal, the precis-side guards read off
``meta.candidate_ref``, and the read makes no store writes (``store.tx``
— the handler's write-transaction entry — is never opened).

The record/gate semantics live in ``tests/test_trust_gate.py``; this file
only pins the handler wiring.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests._autocatpath import needs_engine

needs_engine()

from precis.dispatch import Hub
from precis.store import Store
from precis_pathway.handler import PathwayHandler

_GRAPH: dict[str, Any] = {
    "directed": True,
    "nodes": [{"id": "R"}, {"id": "M"}, {"id": "P"}],
    "links": [
        {
            "source": "R",
            "target": "M",
            "barrier": 0.5,
            "delta_e": -0.1,
            "kind": "reaction",
        },
        {
            "source": "M",
            "target": "P",
            "barrier": 0.3,
            "delta_e": -0.2,
            "kind": "reaction",
        },
    ],
}
_RECORDS = [
    {
        "step": "R->M",
        "seed": 0,
        "check": "neb_convergence",
        "verdict": "pass",
        "severity": "fatal",
        "evidence": {"ts_image": 3},
        "id": "R->M#s0#neb_convergence",
    },
]
_RESULTS = {
    "pathway": ["R", "M", "P"],
    "target": "P",
    "trust_schema": 1,
    "trust": _RECORDS,
    "route_steps": ["R->M", "M->P"],
    "trust_summary": {"barrier": {"available": True, "blocked_by": []}},
}


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


def test_trust_view_is_registered(store: Store) -> None:
    assert "trust" in _handler(store).accepted_views()


def test_gate_view_uncomputed_says_not_computed(store: Store) -> None:
    h = _handler(store)
    _seed(store, "gate-nograph", {"status": "preview"})
    assert "not computed yet" in h.get(id="gate-nograph", view="trust").body


def test_gate_view_pre_schema_refuses_like_trust(store: Store) -> None:
    h = _handler(store)
    _seed(
        store,
        "gate-legacy",
        {
            "status": "ready",
            "graph": _GRAPH,
            "results": {"pathway": ["R", "M", "P"], "target": "P"},
        },
    )
    body = h.get(id="gate-legacy", view="trust").body
    assert "predates" in body and "trust_schema" in body


def test_gate_view_absurd_guard_reads_candidate(store: Store) -> None:
    """``barrier_absurd`` on the candidate (``meta.candidate_ref``) renders
    the precis-side ``absurd magnitude`` row as ``fail`` — the guard is a
    candidate-side stamp, the pathway meta alone never carries it."""
    h = _handler(store)
    with store.tx() as conn:
        st = store.insert_ref(
            kind="structure",
            slug="gate-absurd-slab",
            title="candidate slab",
            meta={"barrier": 12.3, "barrier_absurd": True, "barrier_fidelity": "neb"},
            conn=conn,
        )
    _seed(
        store,
        "gate-absurd",
        {
            "status": "ready",
            "graph": _GRAPH,
            "results": _RESULTS,
            "rate_Ea": 12.3,
            "candidate_ref": st.id,
        },
    )
    body = h.get(id="gate-absurd", view="trust").body
    row = next(ln for ln in body.splitlines() if ln.startswith("absurd magnitude"))
    assert "fail" in row
    # a fired precis guard blocks even when the engine records are clean
    assert "verdict: BLOCKED" in body


def test_gate_view_makes_no_store_writes(store: Store) -> None:
    """The diagnostic is read-only: it must never open a write
    transaction (``store.tx``), which every handler write path goes
    through."""
    h = _handler(store)
    _seed(
        store,
        "gate-ro",
        {"status": "ready", "graph": _GRAPH, "results": _RESULTS},
    )

    def _no_tx(**_kw: Any) -> Any:
        raise AssertionError("view='trust' opened a write transaction")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(store, "tx", _no_tx)
        body = h.get(id="gate-ro", view="trust").body
    assert "gate ·" in body
