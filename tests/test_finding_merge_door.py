"""``edit(kind='finding', id='fi<loser>', args={'merge_into': 'fi<winner>',
'reason': '…'})`` — the agent door for merging two duplicate claim hubs
(gr684893).

Ruling (Reto 2026-11-15): agents may merge claim hubs through MCP while both
hubs are still at ``'candidate'`` — the guard ``precis taproot merge``
already enforces; past ``'candidate'`` nobody may merge (gr754451). The door mirrors
the paper fold (``id=`` is the loser that retires, ``merge_into`` the
survivor) and reuses :func:`precis.taproot.hub.merge_hubs`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers._finding_merge import format_merge_plan
from precis.handlers.finding import FindingHandler
from precis.runtime import PrecisRuntime
from precis.store.types import ChunkInsert
from precis.taproot import hub as taproot_hub
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import (
    MERGE_COLLAPSE_RELATION,
    attach_evidence,
    link_claims,
    merge_hubs,
    mint_hub,
    rewrite_fi_cites,
)
from tests.workers._helpers import seed_chunk, seed_ref

_SKILLS = Path(__file__).resolve().parents[1] / "src" / "precis" / "data" / "skills"


def _handler(store: Any) -> FindingHandler:
    return FindingHandler(hub=Hub(store=store))


def _pair(store: Any) -> tuple[int, int]:
    """(loser, winner): two live candidate hubs stating the same claim."""
    winner = mint_hub(store, CanonicalClaim(sentence="Winner hub claim.", scope={}))
    loser = mint_hub(store, CanonicalClaim(sentence="Loser hub claim.", scope={}))
    return loser, winner


def _seed_evidence(store: Any, loser: int, winner: int) -> tuple[int, int]:
    """(shared, only_loser) papers: one edge the winner already holds
    (redundant on merge), one only the loser holds (repointed)."""
    shared = seed_ref(store, title="Shared paper", kind="paper")
    only_loser = seed_ref(store, title="Loser-only paper", kind="paper")
    attach_evidence(store, hub_ref_id=loser, paper_ref_id=shared, role="corroborates")
    attach_evidence(store, hub_ref_id=winner, paper_ref_id=shared, role="corroborates")
    attach_evidence(
        store, hub_ref_id=loser, paper_ref_id=only_loser, role="corroborates"
    )
    return shared, only_loser


def _seed_cite(store: Any, loser: int, *, name: str = "mergecite") -> int:
    """A live draft paragraph citing ``[fi<loser>]``; returns its chunk_id."""
    ref, _title = store.drafts.create_draft(name=name, title="Cite draft")
    chunk = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text=f"Claim [fi{loser}] stands.",
        at={"last": True},
    )[0]
    return int(chunk.chunk_id)


def _chunk_text(store: Any, chunk_id: int) -> str:
    with store.pool.connection() as conn:
        return str(
            conn.execute(
                "SELECT text FROM chunks WHERE chunk_id = %s", (chunk_id,)
            ).fetchone()[0]
        )


def _is_retired(store: Any, ref_id: int) -> bool:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT retired_at FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return row[0] is not None


def _relation(store: Any, src: int, dst: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT relation FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
            (src, dst),
        ).fetchone()
    return str(row[0]) if row is not None else None


def _row_counts(store: Any) -> tuple[int, ...]:
    """Row counts of every table a merge writes."""
    with store.pool.connection() as conn:
        return tuple(
            conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("refs", "links", "ref_events", "chunk_events")
        )


def _mark_publish_state(store: Any, ref_id: int, state: str) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO nanopub_publish (claim_ref_id, state) VALUES (%s, %s)",
            (ref_id, state),
        )
        conn.commit()


def _seed_draft(store: Any, name: str, *texts: str) -> list[Any]:
    """A live draft with one paragraph per text; returns its chunks."""
    ref, _title = store.drafts.create_draft(name=name, title=f"Draft {name}")
    return [
        store.drafts.add_chunks(
            ref_id=ref.id, chunk_kind="paragraph", text=text, at={"last": True}
        )[0]
        for text in texts
    ]


def _state(store: Any, *ref_ids: int) -> dict[str, Any]:
    """Everything a merge touches, row by row: every link, every chunk of
    every draft, the event tables and the named refs' retirement."""
    with store.pool.connection() as conn:
        links = conn.execute(
            "SELECT link_id, src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id, "
            "relation FROM links ORDER BY link_id"
        ).fetchall()
        chunks = conn.execute(
            "SELECT c.chunk_id, c.text, c.retired_at IS NULL FROM chunks c "
            "JOIN refs r ON r.ref_id = c.ref_id WHERE r.kind = 'draft' "
            "ORDER BY c.chunk_id"
        ).fetchall()
        ref_events = conn.execute("SELECT count(*) FROM ref_events").fetchone()[0]
        chunk_events = conn.execute("SELECT count(*) FROM chunk_events").fetchone()[0]
        retired = conn.execute(
            "SELECT ref_id, retired_at IS NOT NULL FROM refs "
            "WHERE ref_id = ANY(%s) ORDER BY ref_id",
            (list(ref_ids),),
        ).fetchall()
    return {
        "links": [tuple(r) for r in links],
        "chunks": [tuple(r) for r in chunks],
        "ref_events": ref_events,
        "chunk_events": chunk_events,
        "retired": [tuple(r) for r in retired],
    }


def _hypothesis_hub(store: Any) -> int:
    """A hypothesis proposal: a hub with motivation edges and no evidence."""
    chunks = []
    for title in ("Nanoindentation of nanobud films", "Fullerene-tube hybrids"):
        paper = seed_ref(store, title=title, kind="paper")
        chunks.append(seed_chunk(store, ref_id=paper, text=f"A passage from {title}."))
    resp = _handler(store).put(
        title="Nanoindentation measures a modulus above 9 GPa in nanobud films.",
        hypothesis=True,
        motivation="Both papers attribute stiffness to the covalent junction.",
        testable_by="nanoindentation of a pressed nanobud film versus graphene",
        motivated_by=[f"pc{c}" for c in chunks],
        llm_models=["test-model"],
    )
    return int(resp.body.split("fi", 1)[1].split()[0])


def _chase_finding(store: Any) -> int:
    """A plain finding registered against a source (``cited_in=``)."""
    paper = store.insert_ref(
        kind="paper", slug="mergechase23", title="Chase source paper", meta={}
    )
    store.chunks.insert_chunks(
        paper.id, [ChunkInsert(ord=0, text="Body chunk of the source.", meta={})]
    )
    resp = _handler(store).put(
        title="A chased claim",
        body="A chased claim body text",
        scope={},
        cited_in="mergechase23",
    )
    m = re.search(r"id=(\d+)", resp.body)
    assert m is not None, resp.body
    return int(m.group(1))


def _unknown_id(store: Any) -> int:
    with store.pool.connection() as conn:
        return int(conn.execute("SELECT max(ref_id) FROM refs").fetchone()[0]) + 1000


class TestDryRun:
    def test_returns_the_plan_and_writes_nothing(self, store: Any) -> None:
        loser, winner = _pair(store)
        _seed_evidence(store, loser, winner)
        cite = _seed_cite(store, loser)
        before = _row_counts(store)

        body = (
            _handler(store)
            .edit(
                id=f"fi{loser}",
                merge_into=f"fi{winner}",
                reason="same claim, minted twice",
                dry_run=True,
            )
            .body
        )

        assert f"fi{loser} -> fi{winner}" in body
        assert "dry run" in body
        assert "## edges to repoint (1)" in body
        assert "## edges dropped as redundant (1)" in body
        assert "## draft cites rewritten (1)" in body
        assert f"fi{loser} is retired" in body
        assert f"fi{loser} --{MERGE_COLLAPSE_RELATION}--> fi{winner}" in body
        # The paste-ready apply call, minus dry_run.
        assert f"'merge_into': 'fi{winner}'" in body
        assert _row_counts(store) == before
        assert not _is_retired(store, loser)
        assert _chunk_text(store, cite) == f"Claim [fi{loser}] stands."

    def test_dry_run_via_dispatch(self, runtime_with_store: PrecisRuntime) -> None:
        store = runtime_with_store.store
        assert store is not None
        loser, winner = _pair(store)
        before = _row_counts(store)

        body, is_error = runtime_with_store.dispatch_with_status(
            "edit",
            {
                "kind": "finding",
                "id": f"fi{loser}",
                "dry_run": True,
                "__extras__": {"merge_into": f"fi{winner}", "reason": "same claim"},
            },
        )

        assert not is_error, body
        assert f"fi{loser} -> fi{winner}" in body
        assert _row_counts(store) == before
        assert not _is_retired(store, loser)


class TestApply:
    def test_merges_retires_records_and_repoints_cites(self, store: Any) -> None:
        loser, winner = _pair(store)
        shared, only_loser = _seed_evidence(store, loser, winner)
        cite = _seed_cite(store, loser)

        body = (
            _handler(store)
            .edit(
                id=f"fi{loser}",
                merge_into=f"fi{winner}",
                reason="same claim, minted twice",
            )
            .body
        )

        assert body.startswith(f"merged fi{loser} into fi{winner}")
        assert "dry run" not in body
        assert _is_retired(store, loser)
        assert not _is_retired(store, winner)
        # The collapse record, and the evidence edges on the winner.
        assert _relation(store, loser, winner) == MERGE_COLLAPSE_RELATION
        assert _relation(store, only_loser, winner) == "corroborates"
        assert _relation(store, only_loser, loser) is None
        assert _relation(store, shared, loser) is None
        # Live draft prose now cites the winner.
        assert _chunk_text(store, cite) == f"Claim [fi{winner}] stands."
        # The reason is on the audit trail of both hubs.
        with store.pool.connection() as conn:
            events = conn.execute(
                "SELECT ref_id, payload->>'reason' FROM ref_events "
                "WHERE ref_id = ANY(%s) AND payload->>'reason' IS NOT NULL",
                ([loser, winner],),
            ).fetchall()
        assert {int(r[0]) for r in events} == {loser, winner}
        assert {r[1] for r in events} == {"same claim, minted twice"}

    def test_apply_via_dispatch(self, runtime_with_store: PrecisRuntime) -> None:
        store = runtime_with_store.store
        assert store is not None
        loser, winner = _pair(store)
        _seed_evidence(store, loser, winner)
        cite = _seed_cite(store, loser, name="dispatchcite")

        body, is_error = runtime_with_store.dispatch_with_status(
            "edit",
            {
                "kind": "finding",
                "id": f"fi{loser}",
                "__extras__": {"merge_into": f"fi{winner}", "reason": "same claim"},
            },
        )

        assert not is_error, body
        assert f"merged fi{loser} into fi{winner}" in body
        assert _is_retired(store, loser)
        assert _relation(store, loser, winner) == MERGE_COLLAPSE_RELATION
        assert _chunk_text(store, cite) == f"Claim [fi{winner}] stands."

    def test_rerun_is_a_reported_no_op(self, store: Any) -> None:
        loser, winner = _pair(store)
        h = _handler(store)
        h.edit(id=f"fi{loser}", merge_into=f"fi{winner}", reason="same claim")
        before = _row_counts(store)

        body = h.edit(
            id=f"fi{loser}", merge_into=f"fi{winner}", reason="same claim"
        ).body

        assert "already merged" in body
        assert _row_counts(store) == before

    def test_rerun_via_dispatch_reaches_the_retired_loser(
        self, runtime_with_store: PrecisRuntime
    ) -> None:
        """A retired loser's ``fi`` handle no longer resolves as a live ref;
        the door must still see it and report the no-op, not NotFound."""
        store = runtime_with_store.store
        assert store is not None
        loser, winner = _pair(store)
        payload = {
            "kind": "finding",
            "id": f"fi{loser}",
            "__extras__": {"merge_into": f"fi{winner}", "reason": "same claim"},
        }
        _body, is_error = runtime_with_store.dispatch_with_status("edit", dict(payload))
        assert not is_error, _body
        before = _row_counts(store)

        body, is_error = runtime_with_store.dispatch_with_status("edit", dict(payload))

        assert not is_error, body
        assert "already merged" in body
        assert _row_counts(store) == before


class TestRefusals:
    @pytest.mark.parametrize(
        "state", ["reviewed", "superseded", "retracted", "rejected"]
    )
    @pytest.mark.parametrize("side", ["loser", "winner"])
    @pytest.mark.parametrize("dry_run", [True, False])
    def test_past_candidate_is_refused_and_keeps_the_past_candidate_hub(
        self, store: Any, side: str, dry_run: bool, state: str
    ) -> None:
        """Whichever side the caller named, the supersede call retires the
        candidate-state hub and keeps the one past ``'candidate'``."""
        loser, winner = _pair(store)
        _seed_evidence(store, loser, winner)
        kept, dup = (loser, winner) if side == "loser" else (winner, loser)
        _mark_publish_state(store, kept, state)
        before = _row_counts(store)

        with pytest.raises(BadInput, match="past 'candidate'") as ei:
            _handler(store).edit(
                id=f"fi{loser}",
                merge_into=f"fi{winner}",
                reason="same claim",
                dry_run=dry_run,
            )

        supersede = f"delete(kind='finding', id='fi{dup}', superseded_by='fi{kept}'"
        assert "no merge is possible" in ei.value.cause
        assert supersede in str(ei.value.next)
        assert f"id='fi{kept}', superseded_by" not in str(ei.value.next)
        assert "swap" not in str(ei.value.next)
        assert "taproot merge" not in ei.value.cause
        assert "taproot merge" not in str(ei.value.next)
        assert state in ei.value.cause
        assert _row_counts(store) == before
        assert not _is_retired(store, loser)

    @pytest.mark.parametrize("dry_run", [True, False])
    def test_both_past_candidate_prints_no_delete_call(
        self, store: Any, dry_run: bool
    ) -> None:
        loser, winner = _pair(store)
        _mark_publish_state(store, loser, "reviewed")
        _mark_publish_state(store, winner, "reviewed")
        before = _row_counts(store)

        with pytest.raises(BadInput, match="past 'candidate'") as ei:
            _handler(store).edit(
                id=f"fi{loser}",
                merge_into=f"fi{winner}",
                reason="same claim",
                dry_run=dry_run,
            )

        nxt = str(ei.value.next)
        assert "neither can be merged or retired" in nxt
        assert "report the duplicate to the owner" in nxt
        assert "delete(" not in nxt
        assert "taproot merge" not in nxt
        assert _row_counts(store) == before
        assert not _is_retired(store, loser)
        assert not _is_retired(store, winner)

        plan = merge_hubs(store, loser_ref_id=loser, winner_ref_id=winner, dry_run=True)
        view = format_merge_plan(plan)
        assert "neither can be merged or retired" in view
        assert "delete(" not in view
        with pytest.raises(BadInput) as hub_err:
            merge_hubs(store, loser_ref_id=loser, winner_ref_id=winner)
        assert "neither can be merged or retired" in str(hub_err.value.next)
        assert "delete(" not in str(hub_err.value.next)

    @pytest.mark.parametrize("past", ["loser", "winner"])
    @pytest.mark.parametrize("dry_run", [True, False])
    def test_past_candidate_refusal_through_merge_hubs_names_supersede(
        self, store: Any, past: str, dry_run: bool
    ) -> None:
        """The operator CLI runs ``merge_hubs``: same refusal, same pointer
        (the past-candidate hub is kept), no ``precis taproot merge``
        hand-off to a dead end."""
        loser, winner = _pair(store)
        kept, dup = (loser, winner) if past == "loser" else (winner, loser)
        _mark_publish_state(store, kept, "reviewed")
        before = _row_counts(store)
        call = f"delete(kind='finding', id='fi{dup}', superseded_by='fi{kept}'"

        if dry_run:
            plan = merge_hubs(
                store, loser_ref_id=loser, winner_ref_id=winner, dry_run=True
            )
            assert not plan.can_merge
            view = format_merge_plan(plan)
            assert "No merge is possible" in view
            assert call in view
            assert "taproot merge" not in view
            assert "swap" not in view
        else:
            with pytest.raises(BadInput) as ei:
                merge_hubs(store, loser_ref_id=loser, winner_ref_id=winner)
            nxt = str(ei.value.next)
            assert "no merge is possible" in nxt
            assert call in nxt
            assert "swap" not in nxt
        assert _row_counts(store) == before
        assert not _is_retired(store, loser)

    def test_the_named_supersede_call_works_past_candidate(self, store: Any) -> None:
        """The pointer must lead somewhere: the printed delete call retires
        the duplicate and records the survivor even when the survivor is
        past ``'candidate'``."""
        loser, winner = _pair(store)
        _mark_publish_state(store, winner, "reviewed")

        _handler(store).delete(
            id=loser,
            superseded_by=f"fi{winner}",
            reason="same claim",
        )

        assert _is_retired(store, loser)
        assert not _is_retired(store, winner)

    @pytest.mark.parametrize("shape", ["hypothesis", "chase", "unknown"])
    @pytest.mark.parametrize("dry_run", [True, False])
    def test_loser_must_be_a_plain_claim_hub(
        self, store: Any, shape: str, dry_run: bool
    ) -> None:
        """A hypothesis proposal carries motivation edges, never evidence;
        a chase finding is not a hub; an unknown id is nothing at all. None
        may be folded into a claim hub."""
        winner = mint_hub(store, CanonicalClaim(sentence="Winner hub claim.", scope={}))
        loser = {
            "hypothesis": _hypothesis_hub,
            "chase": _chase_finding,
            "unknown": _unknown_id,
        }[shape](store)
        before = _state(store, loser, winner)

        with pytest.raises(BadInput):
            _handler(store).edit(
                id=f"fi{loser}",
                merge_into=f"fi{winner}",
                reason="same claim",
                dry_run=dry_run,
            )

        assert _state(store, loser, winner) == before

    @pytest.mark.parametrize("side", ["loser", "winner"])
    @pytest.mark.parametrize("dry_run", [True, False])
    def test_a_hypothesis_on_either_side_is_refused(
        self, store: Any, side: str, dry_run: bool
    ) -> None:
        """Folding a conjecture into a claim hub would hang motivation edges
        on evidence-bearing ground; folding a claim into a conjecture would
        hand it evidence. The refusal names the hypothesis side."""
        hypothesis = _hypothesis_hub(store)
        plain = mint_hub(store, CanonicalClaim(sentence="Plain hub claim.", scope={}))
        paper = seed_ref(store, title="Plain hub evidence", kind="paper")
        attach_evidence(
            store, hub_ref_id=plain, paper_ref_id=paper, role="corroborates"
        )
        loser, winner = (hypothesis, plain) if side == "loser" else (plain, hypothesis)
        before = _state(store, loser, winner)

        with pytest.raises(BadInput, match="hypothesis hub") as ei:
            _handler(store).edit(
                id=f"fi{loser}",
                merge_into=f"fi{winner}",
                reason="same claim",
                dry_run=dry_run,
            )

        assert f"the {side} fi{hypothesis} is a hypothesis hub" in ei.value.cause
        assert f"fi{plain} is a hypothesis" not in ei.value.cause
        assert "precis-nanopub-help" in str(ei.value.next)
        assert _state(store, loser, winner) == before

    @pytest.mark.parametrize("side", ["loser", "winner"])
    @pytest.mark.parametrize("dry_run", [True, False])
    def test_merge_hubs_itself_refuses_a_hypothesis(
        self, store: Any, side: str, dry_run: bool
    ) -> None:
        """The guard sits where the plan is built, so the CLI and the claim
        page, which call ``merge_hubs`` directly, refuse the same way."""
        hypothesis = _hypothesis_hub(store)
        plain = mint_hub(store, CanonicalClaim(sentence="Plain hub claim.", scope={}))
        loser, winner = (hypothesis, plain) if side == "loser" else (plain, hypothesis)
        before = _state(store, loser, winner)

        with pytest.raises(
            BadInput, match=f"the {side} fi{hypothesis} is a hypothesis"
        ):
            merge_hubs(store, loser_ref_id=loser, winner_ref_id=winner, dry_run=dry_run)

        assert _state(store, loser, winner) == before

    def test_reason_is_required(self, store: Any) -> None:
        loser, winner = _pair(store)
        with pytest.raises(BadInput, match="reason") as ei:
            _handler(store).edit(id=f"fi{loser}", merge_into=f"fi{winner}")
        assert "'merge_into'" in str(ei.value.next)
        assert not _is_retired(store, loser)

    def test_exclusive_with_the_other_edit_kwargs(self, store: Any) -> None:
        loser, winner = _pair(store)
        with pytest.raises(BadInput, match="exclusive"):
            _handler(store).edit(
                id=f"fi{loser}",
                merge_into=f"fi{winner}",
                reason="same claim",
                title="A reworded claim.",
            )
        assert not _is_retired(store, loser)

    def test_one_loser_per_call(self, store: Any) -> None:
        loser, winner = _pair(store)
        other = mint_hub(store, CanonicalClaim(sentence="Other hub claim.", scope={}))
        with pytest.raises(BadInput, match="one loser"):
            _handler(store).edit(
                id=[f"fi{loser}", f"fi{other}"],
                merge_into=f"fi{winner}",
                reason="same claim",
            )
        assert not _is_retired(store, loser)
        assert not _is_retired(store, other)

    @pytest.mark.parametrize("dry_run", [True, False])
    def test_a_hub_cannot_merge_into_itself(self, store: Any, dry_run: bool) -> None:
        loser, _winner = _pair(store)
        before = _state(store, loser)
        with pytest.raises(BadInput, match="itself"):
            _handler(store).edit(
                id=f"fi{loser}",
                merge_into=f"fi{loser}",
                reason="same claim",
                dry_run=dry_run,
            )
        assert _state(store, loser) == before

    def test_winner_must_be_a_live_claim_hub(self, store: Any) -> None:
        loser, _winner = _pair(store)
        paper = seed_ref(store, title="Not a hub", kind="paper")
        with pytest.raises(BadInput, match="claim hub"):
            _handler(store).edit(id=f"fi{loser}", merge_into=paper, reason="same claim")
        assert not _is_retired(store, loser)


class TestRollback:
    @pytest.mark.parametrize("step", ["retire_ref", "append_event"])
    def test_a_late_failure_leaves_nothing_behind(
        self, store: Any, monkeypatch: pytest.MonkeyPatch, step: str
    ) -> None:
        """``retire_ref`` runs after the edge repoints, the cite rewrites
        and the collapse record; ``append_event`` is the door's last write.
        Either one raising must undo every earlier write of the apply."""
        loser, winner = _pair(store)
        _seed_evidence(store, loser, winner)
        _seed_draft(store, "rollbackcite", f"Claim [fi{loser}] stands.")
        before = _state(store, loser, winner)

        def _boom(*_a: Any, **_kw: Any) -> Any:
            raise RuntimeError(f"{step} failed")

        monkeypatch.setattr(store, step, _boom)
        with pytest.raises(RuntimeError, match=f"{step} failed"):
            _handler(store).edit(
                id=f"fi{loser}", merge_into=f"fi{winner}", reason="same claim"
            )
        monkeypatch.undo()

        assert _state(store, loser, winner) == before


class TestCiteRewriteEdges:
    def test_only_the_exact_handle_is_rewritten(self, store: Any) -> None:
        """``fi<loser>7`` and ``fi9<loser>`` share the loser's digits; they
        name other hubs and must survive untouched."""
        loser, winner = _pair(store)
        mixed, longer_only = _seed_draft(
            store,
            "prefixcite",
            f"Both [fi{loser}] and [fi{loser}7] hold; so does [fi9{loser}].",
            f"Only [fi{loser}7] here.",
        )
        events_before = _state(store)["chunk_events"]

        body = (
            _handler(store)
            .edit(id=f"fi{loser}", merge_into=f"fi{winner}", reason="same claim")
            .body
        )

        assert "## draft cites rewritten (1)" in body
        assert _chunk_text(store, mixed.chunk_id) == (
            f"Both [fi{winner}] and [fi{loser}7] hold; so does [fi9{loser}]."
        )
        assert _chunk_text(store, longer_only.chunk_id) == f"Only [fi{loser}7] here."
        assert _state(store)["chunk_events"] == events_before + 1

    def test_a_retired_draft_chunk_is_not_touched(self, store: Any) -> None:
        loser, winner = _pair(store)
        live, retired = _seed_draft(
            store,
            "retiredcite",
            f"Live claim [fi{loser}] stands.",
            f"Retired claim [fi{loser}] stood.",
        )
        store.drafts.retire_chunk(retired.handle)
        events_before = _state(store)["chunk_events"]

        body = (
            _handler(store)
            .edit(id=f"fi{loser}", merge_into=f"fi{winner}", reason="same claim")
            .body
        )

        assert "## draft cites rewritten (1)" in body
        assert _chunk_text(store, live.chunk_id) == f"Live claim [fi{winner}] stands."
        assert _chunk_text(store, retired.chunk_id) == (
            f"Retired claim [fi{loser}] stood."
        )
        assert _state(store)["chunk_events"] == events_before + 1
        assert _is_retired(store, loser)

    def test_a_concurrent_edit_of_a_cited_chunk_is_not_overwritten(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Another session edits the citing chunk after the plan read it and
        before the merge writes it: the merge refuses and the newer text
        stays (gr754451)."""
        loser, winner = _pair(store)
        (cited,) = _seed_draft(store, "racecite", f"Claim [fi{loser}] stands.")
        newer = f"Claim [fi{loser}] stands, as revised."
        plan_cite_rewrites = taproot_hub._plan_cite_rewrites

        def _plan_then_race(conn: Any, **kw: Any) -> Any:
            rewrites = plan_cite_rewrites(conn, **kw)
            store.drafts.edit_text(cited.handle, newer)
            return rewrites

        monkeypatch.setattr(taproot_hub, "_plan_cite_rewrites", _plan_then_race)
        with pytest.raises(BadInput, match="changed since"):
            merge_hubs(store, loser_ref_id=loser, winner_ref_id=winner)
        monkeypatch.undo()

        assert _chunk_text(store, cited.chunk_id) == newer
        assert not _is_retired(store, loser)

    def test_a_list_cite_is_rewritten_inside_the_list(self) -> None:
        assert rewrite_fi_cites("[fi1, fi3]", 1, 2) == "[fi2, fi3]"
        assert rewrite_fi_cites("[fi3 and fi1]", 1, 2) == "[fi3 and fi2]"
        assert rewrite_fi_cites("[fi1, fi2; fi3]", 1, 2) == "[fi2; fi3]"
        assert rewrite_fi_cites("[fi10, fi3]", 1, 2) == "[fi10, fi3]"

    def test_the_retire_guard_sees_a_list_cite(self, store: Any) -> None:
        loser, _winner = _pair(store)
        other = mint_hub(store, CanonicalClaim(sentence="Other hub claim.", scope={}))
        _seed_draft(store, "listguard", f"Two hubs agree [fi{other}; fi{loser}].")

        with pytest.raises(BadInput, match="still cited"):
            store.retire_ref(loser)

        assert not _is_retired(store, loser)

    def test_the_guard_ignores_a_longer_handle_sharing_the_digits(
        self, store: Any
    ) -> None:
        """``fi<loser>3`` names another hub: retiring ``fi<loser>`` is not
        refused over it, alone or inside a list."""
        loser, _winner = _pair(store)
        _seed_draft(
            store,
            "prefixguard",
            f"One [fi{loser}3], two [fi4, fi{loser}3] and three [fi9{loser}; fi4].",
        )

        store.retire_ref(loser)

        assert _is_retired(store, loser)

    def test_a_list_already_naming_the_winner_keeps_one_cite(self, store: Any) -> None:
        loser, winner = _pair(store)
        other = mint_hub(store, CanonicalClaim(sentence="Other hub claim.", scope={}))
        (listed,) = _seed_draft(
            store, "duplist", f"All agree [fi{winner}, fi{other} and fi{loser}]."
        )

        _handler(store).edit(
            id=f"fi{loser}", merge_into=f"fi{winner}", reason="same claim"
        )

        assert _chunk_text(store, listed.chunk_id) == (
            f"All agree [fi{winner}, fi{other}]."
        )
        assert _is_retired(store, loser)

    def test_a_list_cite_does_not_dangle_after_the_merge(self, store: Any) -> None:
        loser, winner = _pair(store)
        other = mint_hub(store, CanonicalClaim(sentence="Other hub claim.", scope={}))
        (listed,) = _seed_draft(
            store, "listcite", f"Two hubs agree [fi{loser}, fi{other}]."
        )

        try:
            _handler(store).edit(
                id=f"fi{loser}", merge_into=f"fi{winner}", reason="same claim"
            )
        except BadInput:
            # Refusing and listing the cite is the other acceptable outcome.
            assert not _is_retired(store, loser)
            return

        assert f"fi{loser}" not in _chunk_text(store, listed.chunk_id)


class TestRepointRelationChecks:
    """A repointed hub-to-hub edge passes the checks the mint door
    (``link_claims``) runs, or the merge is blocked and names the edge
    (gr754451)."""

    @pytest.mark.parametrize("dry_run", [True, False])
    def test_a_disputes_edge_the_winner_may_not_hold_blocks_the_merge(
        self, store: Any, dry_run: bool
    ) -> None:
        winner = mint_hub(
            store,
            CanonicalClaim(
                sentence="Winner hub claim.", scope={}, claim_type="landscape"
            ),
        )
        loser, other = (
            mint_hub(
                store, CanonicalClaim(sentence=s, scope={}, claim_type="measurement")
            )
            for s in ("Loser hub claim.", "Other hub claim.")
        )
        link_claims(
            store, from_hub_ref_id=loser, to_hub_ref_id=other, relation="disputes"
        )
        edge = f"fi{loser} --disputes--> fi{other}"
        before = _state(store, loser, winner)

        plan = merge_hubs(store, loser_ref_id=loser, winner_ref_id=winner, dry_run=True)
        assert not plan.can_merge
        assert edge in str(plan.block_reason)

        with pytest.raises(BadInput) as ei:
            _handler(store).edit(
                id=f"fi{loser}",
                merge_into=f"fi{winner}",
                reason="same claim",
                dry_run=dry_run,
            )
        assert edge in ei.value.cause
        assert "past 'candidate'" not in ei.value.cause
        assert "taproot merge --loser" not in str(ei.value.next)
        with pytest.raises(BadInput, match=re.escape(edge)):
            merge_hubs(store, loser_ref_id=loser, winner_ref_id=winner)
        assert _state(store, loser, winner) == before

    @pytest.mark.parametrize("direction", ["outbound", "inbound"])
    @pytest.mark.parametrize("dry_run", [True, False])
    def test_an_explains_edge_that_would_close_a_cycle_blocks_the_merge(
        self, store: Any, dry_run: bool, direction: str
    ) -> None:
        loser, winner = _pair(store)
        other = mint_hub(store, CanonicalClaim(sentence="Other hub claim.", scope={}))
        if direction == "outbound":
            pairs = [(loser, other), (other, winner)]
            edge = f"fi{loser} --explains--> fi{other}"
        else:
            pairs = [(other, loser), (winner, other)]
            edge = f"fi{other} --explains--> fi{loser}"
        for src, dst in pairs:
            link_claims(
                store, from_hub_ref_id=src, to_hub_ref_id=dst, relation="explains"
            )
        before = _state(store, loser, winner)

        plan = merge_hubs(store, loser_ref_id=loser, winner_ref_id=winner, dry_run=True)
        assert not plan.can_merge
        assert edge in str(plan.block_reason)

        with pytest.raises(BadInput) as ei:
            _handler(store).edit(
                id=f"fi{loser}",
                merge_into=f"fi{winner}",
                reason="same claim",
                dry_run=dry_run,
            )
        assert edge in ei.value.cause
        assert _state(store, loser, winner) == before

    def test_an_edge_the_winner_may_hold_is_still_repointed(self, store: Any) -> None:
        loser, winner = _pair(store)
        other = mint_hub(store, CanonicalClaim(sentence="Other hub claim.", scope={}))
        link_claims(
            store, from_hub_ref_id=loser, to_hub_ref_id=other, relation="disputes"
        )
        link_claims(
            store, from_hub_ref_id=other, to_hub_ref_id=loser, relation="explains"
        )

        _handler(store).edit(
            id=f"fi{loser}", merge_into=f"fi{winner}", reason="same claim"
        )

        assert _is_retired(store, loser)
        assert _relation(store, winner, other) == "disputes"
        assert _relation(store, other, winner) == "explains"

    @pytest.mark.parametrize("relation", ["disputes", "explains"])
    def test_the_refusal_gives_a_removal_call_that_unblocks_the_merge(
        self, runtime_with_store: PrecisRuntime, relation: str
    ) -> None:
        """The refusal names the edge as it stands on the loser and the
        ``link`` call that removes it; that call, through the MCP surface,
        lets the merge through."""
        store = runtime_with_store.store
        assert store is not None
        winner = mint_hub(
            store,
            CanonicalClaim(
                sentence="Winner hub claim.", scope={}, claim_type="landscape"
            ),
        )
        loser, other = (
            mint_hub(
                store, CanonicalClaim(sentence=s, scope={}, claim_type="measurement")
            )
            for s in ("Loser hub claim.", "Other hub claim.")
        )
        if relation == "disputes":
            src, dst = loser, other
        else:
            src, dst = other, loser
            link_claims(
                store, from_hub_ref_id=winner, to_hub_ref_id=other, relation=relation
            )
        link_claims(store, from_hub_ref_id=src, to_hub_ref_id=dst, relation=relation)

        with pytest.raises(BadInput) as ei:
            _handler(store).edit(
                id=f"fi{loser}", merge_into=f"fi{winner}", reason="same claim"
            )

        assert f"fi{src} --{relation}--> fi{dst}" in ei.value.cause
        steps = str(ei.value.next)
        assert (
            f"link(kind='finding', id='fi{src}', target='fi{dst}', "
            f"rel='{relation}', mode='remove')"
        ) in steps
        assert "relink" not in steps

        body, is_error = runtime_with_store.dispatch_with_status(
            "link",
            {
                "kind": "finding",
                "id": f"fi{src}",
                "target": f"fi{dst}",
                "rel": relation,
                "mode": "remove",
            },
        )
        assert not is_error, body
        _handler(store).edit(
            id=f"fi{loser}", merge_into=f"fi{winner}", reason="same claim"
        )
        assert _is_retired(store, loser)


class TestMergePlanViewNamesTheDoor:
    def test_the_read_only_plan_points_at_the_edit_door(self, store: Any) -> None:
        loser, winner = _pair(store)
        body = (
            _handler(store).get(id=winner, view="merge-plan", loser=f"fi{loser}").body
        )
        assert f"edit(kind='finding', id='fi{loser}'" in body
        assert f"'merge_into': 'fi{winner}'" in body
        assert "human door" not in body


@pytest.mark.parametrize(
    "skill", ["precis-taproot-hub-edit-help.md", "precis-finding-help.md"]
)
def test_skills_describe_the_door(skill: str) -> None:
    text = (_SKILLS / skill).read_text(encoding="utf-8")
    assert '"merge_into"' in text
    assert "no merge verb" not in text
