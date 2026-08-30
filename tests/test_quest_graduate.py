"""Tests for quest graduation — the in-silico ceiling (slice 4e).

Covers the graduation rule (from ``meta.graduation``), graduating a frontier
candidate that crosses the ceiling (tag + `milestone` deed, idempotent), the
no-rule no-op, and the `needs-experiment` gap the slice-3 queue then surfaces.
Runs against real PG (the ``store`` fixture).
"""

from __future__ import annotations

import re
from typing import Any

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest import compute as compute_mod
from precis.quest import graduate as grad
from precis.quest.gaps import quest_gaps

_SPEC = {
    "cell": {"a": 8.4, "b": 8.4, "c": 24.0},
    "ops": [{"op": "add_atom", "element": "Fe", "frac": [0.0, 0.0, 0.5]}],
}


def _mk_quest(store: Any, text: str) -> int:
    resp = QuestHandler(hub=Hub(store=store)).put(text=text)
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, resp.body
    return int(m.group(1))


def _candidate_with_energy(
    store: Any, qid: int, spec: dict[str, Any], energy: float
) -> int:
    sid = compute_mod.ensure_candidate(store, qid, {"name": "cand", "structure": spec})
    assert sid is not None
    store.structure_record_run(
        sid,
        fidelity="ml",
        on_version=1,
        converged=True,
        n_steps=10,
        max_disp=0.0,
        energy=energy,
    )
    return sid


def _set_rule(store: Any, qid: int, **rule: Any) -> None:
    from precis.quest.cascade import _merge_meta

    _merge_meta(store, qid, {"graduation": rule})


class TestGraduation:
    def test_no_rule_is_noop(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        _candidate_with_energy(store, qid, _SPEC, -20.0)
        assert grad.graduate_frontier(store, qid) == []

    def test_crossing_the_ceiling_graduates(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        sid = _candidate_with_energy(store, qid, _SPEC, -20.0)
        _set_rule(store, qid, key="energy", sense="min", threshold=-15.0)
        graduated = grad.graduate_frontier(store, qid)
        assert graduated == [sid]
        assert any(str(t) == "needs-experiment" for t in store.tags_for(sid))
        logs = [
            b
            for b in store.chunks.list_chunks_for_ref(qid)
            if b.chunk_kind == "quest_log"
        ]
        assert any(
            (b.meta or {}).get("entry_type") == "milestone" and "graduated" in b.text
            for b in logs
        )
        # gr-linkify: the handle is BRACKETED (a bare handle only linkifies
        # inside `[...]` — see BARE_BRACKET_REF_PATTERN in
        # precis.utils.mentions) while the candidate name ("cand") stays in
        # plain parens right after it.
        assert any(f"graduated [st{sid}] (cand)" in b.text for b in logs)
        # idempotent — a second call does not re-graduate
        assert grad.graduate_frontier(store, qid) == []

    def test_below_the_ceiling_does_not_graduate(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        _candidate_with_energy(store, qid, _SPEC, -10.0)  # not < -15
        _set_rule(store, qid, key="energy", sense="min", threshold=-15.0)
        assert grad.graduate_frontier(store, qid) == []

    def test_graduated_candidate_surfaces_as_gap(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        sid = _candidate_with_energy(store, qid, _SPEC, -20.0)
        _set_rule(store, qid, key="energy", sense="min", threshold=-15.0)
        grad.graduate_frontier(store, qid)
        kinds = [g.kind for g in quest_gaps(store, qid)]
        assert "needs-experiment" in kinds
        exp = next(g for g in quest_gaps(store, qid) if g.kind == "needs-experiment")
        assert exp.handle == f"st{sid}"


class TestBarrierQualityGate:
    """A candidate that crosses a autocatpath barrier ceiling but whose pathway
    did not converge (harvest stamped ``barrier_trusted=False``) is held
    back — the pathway warnings gate below OPEN-ITEMS/decided spec.

    Ranking exclusion (frontier.py) is now the primary mechanism: an untrusted
    barrier is dropped from ``measures`` at frontier-build time, so the
    candidate falls to ``unevaluated`` and ``graduate_frontier`` (which only
    walks ``fr.frontier``) never even sees it — no tag, no note, from *this*
    path. The explicit ``barrier_trusted is False`` check in ``graduate.py``
    is deliberately kept as a belt-and-suspenders defense (see
    ``test_defensive_gate_fires_if_frontier_ever_yields_an_untrusted_candidate``
    below), in case a future frontier change ever lets one through.
    """

    def _candidate_with_barrier(
        self, store: Any, qid: int, barrier: float, *, trusted: bool | None
    ) -> int:
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "cand", "structure": _SPEC}
        )
        assert sid is not None
        # A frontier candidate must be converged + carry the objective measure.
        store.structure_record_run(
            sid,
            fidelity="ml",
            on_version=1,
            converged=True,
            n_steps=10,
            max_disp=0.0,
            energy=-10.0,
        )
        meta: dict[str, Any] = {"barrier": barrier}
        if trusted is not None:
            meta["barrier_trusted"] = trusted
        store.stamp_ref_meta(sid, meta)
        return sid

    def test_untrusted_barrier_is_excluded_from_ranking_and_not_graduated(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "Lowest-barrier Pd catalyst")
        store.stamp_ref_meta(
            qid, {"rubric_objectives": [{"key": "barrier", "sense": "min"}]}
        )
        sid = self._candidate_with_barrier(store, qid, 0.3, trusted=False)
        _set_rule(store, qid, key="barrier", sense="min", threshold=0.5)
        graduated = grad.graduate_frontier(store, qid)
        assert graduated == []
        assert not any(str(t) == "needs-experiment" for t in store.tags_for(sid))

        from precis.quest.frontier import quest_frontier

        fr = quest_frontier(store, qid)
        assert sid not in [c.ref_id for c in fr.frontier]
        assert sid not in [c.ref_id for c in fr.dominated]
        # Not silently "never tried" either — it's visible+marked as
        # provisional (measured, unconfirmed), just never confirmed/ranked.
        assert sid not in [c.ref_id for c in fr.unevaluated]
        assert sid in [pc.candidate.ref_id for pc in fr.provisional]

    def test_defensive_gate_fires_if_frontier_ever_yields_an_untrusted_candidate(
        self, store: Any, monkeypatch: Any
    ) -> None:
        """Belt-and-suspenders: if ``quest_frontier`` ever handed
        ``graduate_frontier`` a frontier candidate whose barrier is untrusted
        (should not happen given frontier.py's ranking exclusion, but this is
        the defensive check that survives a future regression there), it must
        still be held back with a `note` — never silently graduated."""
        from precis.quest import frontier as frontier_mod

        qid = _mk_quest(store, "Lowest-barrier Pd catalyst")
        sid = self._candidate_with_barrier(store, qid, 0.3, trusted=False)
        _set_rule(store, qid, key="barrier", sense="min", threshold=0.5)

        fake_candidate = frontier_mod.Candidate(
            ref_id=sid,
            handle=f"st{sid}",
            name="synthetic",
            measures={"barrier": 0.3},  # bypasses the usual exclusion
            converged=True,
            flags={
                "barrier_trusted": False,
                "barrier_neb_failed": 1,
                "barrier_desorbed": 2,
            },
        )
        fake_fr = frontier_mod.FrontierResult(
            objectives=[("barrier", "min")], frontier=[fake_candidate]
        )
        monkeypatch.setattr(frontier_mod, "quest_frontier", lambda *a, **k: fake_fr)
        graduated = grad.graduate_frontier(store, qid)
        assert graduated == []
        assert not any(str(t) == "needs-experiment" for t in store.tags_for(sid))
        logs = [
            b
            for b in store.chunks.list_chunks_for_ref(qid)
            if b.chunk_kind == "quest_log"
        ]
        assert any(
            (b.meta or {}).get("entry_type") == "note" and "held back" in b.text
            for b in logs
        )
        # gr-linkify: bracketed handle, name ("synthetic") still in parens.
        assert any(f"held back [st{sid}] (synthetic)" in b.text for b in logs)

    def test_trusted_barrier_graduates(self, store: Any) -> None:
        qid = _mk_quest(store, "Lowest-barrier Pd catalyst")
        store.stamp_ref_meta(
            qid, {"rubric_objectives": [{"key": "barrier", "sense": "min"}]}
        )
        sid = self._candidate_with_barrier(store, qid, 0.3, trusted=True)
        _set_rule(store, qid, key="barrier", sense="min", threshold=0.5)
        graduated = grad.graduate_frontier(store, qid)
        assert graduated == [sid]
        assert any(str(t) == "needs-experiment" for t in store.tags_for(sid))

    def test_unknown_trust_graduates(self, store: Any) -> None:
        """No trust flag stamped (legacy / pre-gate harvest) → not blocked."""
        qid = _mk_quest(store, "Lowest-barrier Pd catalyst")
        store.stamp_ref_meta(
            qid, {"rubric_objectives": [{"key": "barrier", "sense": "min"}]}
        )
        sid = self._candidate_with_barrier(store, qid, 0.3, trusted=None)
        _set_rule(store, qid, key="barrier", sense="min", threshold=0.5)
        graduated = grad.graduate_frontier(store, qid)
        assert graduated == [sid]


class TestTierLadderGraduationGate:
    """A ``meta.fidelity_ladder`` quest additionally requires the candidate's
    canonical barrier to have come from a TRUSTED verify-tier (coadsorbed)
    pathway before it graduates — a barrier that only crossed the ceiling on
    the cheaper parked/neb tier is held back as "pending verify", not
    graduated. A ladder-off quest (no ``meta.fidelity_ladder``) is unaffected —
    today's straight-to-NEB graduation stays exactly as tested above."""

    def _candidate_with_tiered_barrier(
        self,
        store: Any,
        qid: int,
        barrier: float,
        *,
        barrier_fidelity: str | None,
        trusted: bool = True,
    ) -> int:
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "cand", "structure": _SPEC}
        )
        assert sid is not None
        store.structure_record_run(
            sid,
            fidelity="ml",
            on_version=1,
            converged=True,
            n_steps=10,
            max_disp=0.0,
            energy=-10.0,
        )
        meta: dict[str, Any] = {"barrier": barrier, "barrier_trusted": trusted}
        if barrier_fidelity is not None:
            meta["barrier_fidelity"] = barrier_fidelity
        store.stamp_ref_meta(sid, meta)
        return sid

    def test_neb_tier_barrier_held_back_pending_verify_when_ladder_on(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "Lowest-barrier Pd catalyst")
        store.stamp_ref_meta(
            qid,
            {
                "fidelity_ladder": True,
                "rubric_objectives": [{"key": "barrier", "sense": "min"}],
            },
        )
        sid = self._candidate_with_tiered_barrier(
            store, qid, 0.3, barrier_fidelity="neb", trusted=True
        )
        _set_rule(store, qid, key="barrier", sense="min", threshold=0.5)
        graduated = grad.graduate_frontier(store, qid)
        assert graduated == []
        assert not any(str(t) == "needs-experiment" for t in store.tags_for(sid))
        logs = [
            b
            for b in store.chunks.list_chunks_for_ref(qid)
            if b.chunk_kind == "quest_log"
        ]
        assert any(
            (b.meta or {}).get("entry_type") == "note" and "pending verify" in b.text
            for b in logs
        )
        # gr-linkify: bracketed handle, name ("cand") still in parens after it.
        assert any(f"pending verify: [st{sid}] (cand)" in b.text for b in logs)

    def test_verify_tier_trusted_barrier_graduates_when_ladder_on(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "Lowest-barrier Pd catalyst")
        store.stamp_ref_meta(
            qid,
            {
                "fidelity_ladder": True,
                "rubric_objectives": [{"key": "barrier", "sense": "min"}],
            },
        )
        sid = self._candidate_with_tiered_barrier(
            store, qid, 0.3, barrier_fidelity="verify", trusted=True
        )
        _set_rule(store, qid, key="barrier", sense="min", threshold=0.5)
        graduated = grad.graduate_frontier(store, qid)
        assert graduated == [sid]
        assert any(str(t) == "needs-experiment" for t in store.tags_for(sid))

    def test_ladder_off_quest_graduates_on_a_neb_tier_barrier_unaffected(
        self, store: Any
    ) -> None:
        """No ``meta.fidelity_ladder`` at all (the default, pre-ladder shape) —
        the new verify-only gate never fires, matching every existing
        `TestBarrierQualityGate` assertion above."""
        qid = _mk_quest(store, "Lowest-barrier Pd catalyst")
        store.stamp_ref_meta(
            qid, {"rubric_objectives": [{"key": "barrier", "sense": "min"}]}
        )
        sid = self._candidate_with_tiered_barrier(
            store, qid, 0.3, barrier_fidelity="neb", trusted=True
        )
        _set_rule(store, qid, key="barrier", sense="min", threshold=0.5)
        graduated = grad.graduate_frontier(store, qid)
        assert graduated == [sid]
