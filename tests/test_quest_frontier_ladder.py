"""Tests for the frontier evidence ladder (gr592563).

When ``args={'trusted': true}`` leaves the confirmed frontier empty —
today's qu164903 case: hundreds of converged candidates, every objective
value carried untrusted — ``view='frontier'`` prints an evidence ladder
(the provisional candidates ranked closest-to-trusted, one blocker each)
instead of the bare "no candidates pass" line. Covers the pure
:func:`precis.quest.frontier.evidence_ladder` ranking and the handler
wiring in ``QuestHandler._render_frontier``, reusing the store-fixture
idiom of ``tests/test_quest_results_table.py``.
"""

from __future__ import annotations

import re
from typing import Any

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest import compute as compute_mod
from precis.quest.frontier import (
    Candidate,
    FrontierResult,
    ProvisionalCandidate,
    evidence_ladder,
)
from precis.utils import handle_registry


def _pc(
    ref_id: int,
    *,
    untrusted: frozenset[str],
    reasons: list[str],
    flags: dict[str, Any],
    converged: bool = True,
    on_frontier: bool = False,
) -> ProvisionalCandidate:
    c = Candidate(ref_id, f"st{ref_id}", f"c{ref_id}", {}, converged, flags=flags)
    return ProvisionalCandidate(
        candidate=c,
        measures={k: 0.5 for k in untrusted},
        untrusted_keys=untrusted,
        reasons=reasons,
        on_frontier=on_frontier,
    )


def test_ladder_ranks_by_blockers() -> None:
    """Three provisional candidates — 1 untrusted key, 2 untrusted keys,
    1 untrusted + network-stale — rank in that order (a stale candidate's
    values are all pending re-measurement, so it is furthest from trust
    regardless of how few keys failed), and each rung carries exactly one
    blocker string."""
    one = _pc(
        1,
        untrusted=frozenset({"barrier"}),
        reasons=["barrier untrusted: NEB not converged"],
        flags={"barrier_trusted": False},
    )
    two = _pc(
        2,
        untrusted=frozenset({"barrier", "selectivity_margin"}),
        reasons=["barrier untrusted", "selectivity unavailable"],
        flags={"barrier_trusted": False, "selectivity_trusted": False},
    )
    stale = _pc(
        3,
        untrusted=frozenset({"barrier"}),
        reasons=[
            "barrier untrusted",
            "measured on an older reaction network (acat@3)",
        ],
        flags={
            "barrier_trusted": False,
            "network_stale": True,
            "network_stale_keys": ["barrier"],
        },
    )
    fr = FrontierResult(
        objectives=[("barrier", "min")],
        provisional=[stale, two, one],  # scrambled input order
    )
    rows = evidence_ladder(fr, top_n=10)
    assert [r.provisional.candidate.ref_id for r in rows] == [1, 2, 3]
    assert all(isinstance(r.blocker, str) and r.blocker for r in rows)
    # the stale rung's blocker names the re-measurement, not the gate
    assert "older reaction network" in rows[2].blocker


def test_ladder_top_n_caps() -> None:
    fr = FrontierResult(
        objectives=[("barrier", "min")],
        provisional=[
            _pc(
                i,
                untrusted=frozenset({"barrier"}),
                reasons=["barrier untrusted"],
                flags={"barrier_trusted": False},
            )
            for i in range(15)
        ],
    )
    assert len(evidence_ladder(fr)) == 10
    assert len(evidence_ladder(fr, top_n=15)) == 15


_CELL = {"a": 10.0, "b": 10.0, "c": 20.0, "pbc": [True, True, False]}


def _mk_quest(store: Any) -> int:
    resp = QuestHandler(hub=Hub(store=store)).put(text="A NO→NH₃ catalyst")
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, resp.body
    qid = int(m.group(1))
    store.stamp_ref_meta(
        qid,
        {
            "reaction_config": {"slab": {"element": "Pd"}},
            "rubric_objectives": [{"key": "barrier", "sense": "min"}],
        },
    )
    return qid


def _mk_provisional(store: Any, qid: int, i: int) -> int:
    """A converged candidate whose only objective is carried untrusted —
    one provisional row, the qu164903 case in miniature. ``i`` varies the
    Cu dopant's spot so each spec mints a distinct structure."""
    sid = compute_mod.ensure_candidate(
        store,
        qid,
        {
            "name": f"cand-{i}",
            "structure": {
                "cell": _CELL,
                "ops": [
                    {"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.3]},
                    {
                        "op": "add_atom",
                        "element": "Cu",
                        "frac": [0.05 + 0.04 * i, 0.1, 0.45],
                    },
                ],
            },
        },
    )
    assert sid is not None
    store.structure_record_run(
        sid,
        fidelity="ml",
        on_version=1,
        converged=True,
        n_steps=1,
        max_disp=0.0,
        energy=-1.0,
    )
    store.stamp_ref_meta(sid, {"barrier": 0.3 + 0.01 * i, "barrier_trusted": False})
    return sid


def test_trusted_empty_prints_ladder(store: Any) -> None:
    """``view='frontier' args={'trusted': true}`` on a quest whose only
    candidate is provisional prints the ladder — "evidence ladder" and the
    candidate's handle — and NOT the "no candidates pass" line."""
    qid = _mk_quest(store)
    sid = _mk_provisional(store, qid, 0)

    h = QuestHandler(hub=Hub(store=store))
    handle = handle_registry.try_format("structure", sid)
    assert handle is not None
    body = h.get(id=qid, view="frontier", args={"trusted": True}).body
    assert "evidence ladder" in body
    assert handle in body
    assert "no candidates pass" not in body
    # and the untrusted barrier still reads as untrusted on its rung
    assert "barrier=0.3 ⚠untrusted" in body
    # without the filter the candidate stays in the provisional band
    plain = h.get(id=qid, view="frontier").body
    assert "evidence ladder" not in plain
    assert "provisional (measured, unconfirmed)" in plain


def test_frontier_reports_the_kept_barriers_engine(store: Any) -> None:
    qid = _mk_quest(store)
    pathway = _mk_provisional(store, qid, 0)
    neb = _mk_provisional(store, qid, 1)
    legacy = _mk_provisional(store, qid, 2)
    store.stamp_ref_meta(
        pathway,
        {
            "network_basis": {"version": "9.9.9"},
            "network_basis_by_key": {"barrier": {"version": "0.31.0"}},
        },
    )
    store.stamp_ref_meta(
        neb,
        {
            "barrier_source": "struct_neb",
            "neb_run_id": 10,
            "neb_edge_fs_ref_id": 20,
            "neb_edges": {
                "20": {
                    "fs_ref_id": 20,
                    "neb_run_id": 10,
                    "barrier": 0.31,
                    "engine": "neb-v1",
                },
                "21": {
                    "fs_ref_id": 21,
                    "neb_run_id": 11,
                    "barrier": 0.31,
                    "engine": "neb-v2",
                },
            },
        },
    )
    store.stamp_ref_meta(legacy, {"network_basis": {"version": "9.9.9"}})

    h = QuestHandler(hub=Hub(store=store))
    plain = h.get(id=qid, view="frontier", args={"trusted": True}).body
    assert re.search(r"cand-0 .*engine=0\.31\.0", plain)
    assert re.search(r"cand-1 .*engine=neb-v1", plain)
    assert re.search(r"cand-2 .*engine=—", plain)

    projected = h.get(
        id=qid,
        view="frontier",
        args={"columns": ["name", "engine"], "where": {"engine": "neb-v1"}},
    ).body
    assert "name=cand-1 engine=neb-v1" in projected
    assert "name=cand-0" not in projected


def test_trusted_empty_ladder_limit_widens(store: Any) -> None:
    """12 provisional candidates: the default read prints the top-10 page
    plus the widen hint; ``args={'limit': N}`` shows every rung."""
    qid = _mk_quest(store)
    for i in range(12):
        _mk_provisional(store, qid, i)
    h = QuestHandler(hub=Hub(store=store))
    body = h.get(id=qid, view="frontier", args={"trusted": True}).body
    assert "10 of 12" in body
    assert "widen: get(kind='quest'" in body
    wide = h.get(id=qid, view="frontier", args={"trusted": True, "limit": 12}).body
    assert "12 of 12" in wide
    assert "more provisional" not in wide


def test_trusted_empty_ladder_continues_via_cursor(store: Any) -> None:
    qid = _mk_quest(store)
    for i in range(12):
        _mk_provisional(store, qid, i)
    h = QuestHandler(hub=Hub(store=store))
    first = h.get(id=qid, view="frontier", args={"trusted": True}).body
    import re

    token = re.search(r"'cursor': '([^']+)'", first)
    assert token is not None
    tail = h.get(
        id=qid, view="frontier", args={"trusted": True, "cursor": token.group(1)}
    ).body
    assert "evidence ladder" in tail
    assert tail.count("— clear:") == 2
    assert "next:" not in tail
