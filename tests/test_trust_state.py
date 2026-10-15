"""Graph trust state, slice 1 (migration 0214; graph-node-trust-state.md).

Each test names the slice-1 acceptance line it pins. Writes run in their
own pooled transaction (the revisions sealer and the act triggers fire at
commit / per statement), and every assertion reads after commit.
"""

from __future__ import annotations

from typing import Any, Literal

import psycopg
import pytest

from precis.errors import BadInput
from precis.store import Store
from precis.store.pool import pin_connection
from precis.store.revision_context import (
    TrustAct,
    revision_context,
    trust_producer,
)
from precis.store.types import ChunkInsert


def _memory(store: Store, body: str = "first body", *, title: str = "a memory") -> int:
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="memory", slug=None, title=title, meta={}, conn=conn
        )
        store.chunks.insert_chunks(ref.id, [_chunk(body)], conn=conn)
    return ref.id


def _one(conn: psycopg.Connection, query: str, params: tuple[object, ...] = ()) -> Any:
    """The first column of the first row."""
    row = conn.execute(query, params).fetchone()
    assert row is not None
    return row[0]


def _chunk(text: str) -> ChunkInsert:
    return ChunkInsert(ord=0, text=text, meta={"chunk_kind": "memory_body"})


def _set_title(store: Store, ref_id: int, title: str) -> None:
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET title = %s WHERE ref_id = %s", (title, ref_id))


def _sha(store: Store, kind: str, target_id: int) -> str | None:
    state = store.trust_state(kind, target_id)
    assert state is not None
    return state.sha


def _stamp(store: Store, kind: str, target_id: int) -> tuple[str, str, int]:
    state = store.trust_state(kind, target_id)
    assert state is not None
    return (state.origin, state.confidence, state.version)


def _consider(store: Store, kind: Literal["ref", "link"], target_id: int) -> TrustAct:
    state = store.trust_state(kind, target_id)
    assert state is not None
    return TrustAct(
        action="consider",
        target_kind=kind,
        target_id=target_id,
        expected_sha=state.sha,
        reason="read it against the source",
    )


# ── Values/identity: producer stamps, legacy and compatibility unknown ──────


def test_unconverted_writes_are_unknown_version_zero(store: Store) -> None:
    mid = _memory(store)
    a = store.insert_ref(kind="finding", slug=None, title="claim A", meta={}).id
    lid = store.add_link(src_ref_id=mid, dst_ref_id=a, relation="related-to").id
    assert _stamp(store, "ref", mid) == ("unknown", "unknown", 0)
    assert _stamp(store, "link", lid) == ("unknown", "unknown", 0)
    assert store.trust_acts_for("ref", mid) == []


@pytest.mark.parametrize("origin", ["human", "extracted", "computed", "generated"])
def test_converted_writer_stamps_origin_hunch(store: Store, origin: str) -> None:
    with revision_context(
        "note", actor="t", trust_origin=origin, trust_actor_type="agent"
    ):
        mid = _memory(store)
    assert _stamp(store, "ref", mid) == (origin, "hunch", 1)
    (act,) = store.trust_acts_for("ref", mid)
    assert act["action"] == "create"
    assert act["after"] == {"origin": origin, "confidence": "hunch", "version": 1}
    assert act["actor"] == "t" and act["actor_type"] == "agent"
    assert act["at"].endswith("Z")


def test_generated_under_human_request_stays_generated(store: Store) -> None:
    # A human asked; the model wrote: the producer is generated.
    with revision_context(actor="user:reto"):
        with revision_context(trust_origin="generated", trust_actor_type="agent"):
            mid = _memory(store)
    assert _stamp(store, "ref", mid) == ("generated", "hunch", 1)


def test_trust_producer_does_not_override_a_converted_caller(store: Store) -> None:
    with revision_context(trust_origin="human", trust_actor_type="human"):
        with trust_producer("computed", actor_type="worker"):
            mid = _memory(store)
    with trust_producer("computed", actor_type="worker"):
        other = _memory(store)
    assert _stamp(store, "ref", mid)[0] == "human"
    assert _stamp(store, "ref", other)[0] == "computed"


# ── Values/identity: refusals ──────────────────────────────────────────────


def test_raw_trust_values_are_refused(store: Store) -> None:
    mid = _memory(store)
    with pytest.raises(psycopg.errors.CheckViolation, match="trust: .*write context"):
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET trust_confidence = 'considered' WHERE ref_id = %s",
                (mid,),
            )
    with pytest.raises(psycopg.errors.CheckViolation, match="trust: "):
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO refs (kind, title, trust_origin, trust_confidence, "
                "trust_write_version) VALUES ('memory', 'forged', 'human', 'hunch', 1)"
            )
    assert _stamp(store, "ref", mid) == ("unknown", "unknown", 0)


def test_null_and_invalid_values_are_refused(store: Store) -> None:
    with pytest.raises(
        (psycopg.errors.NotNullViolation, psycopg.errors.CheckViolation)
    ):
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO refs (kind, title, trust_origin) VALUES ('memory', 'x', NULL)"
            )
    with pytest.raises(
        psycopg.errors.CheckViolation, match="invalid write-context origin"
    ):
        with store.pool.connection() as conn:
            conn.execute("SELECT set_config('precis.trust_origin', 'oracle', true)")
            conn.execute("INSERT INTO refs (kind, title) VALUES ('memory', 'x')")
    with pytest.raises(ValueError):
        with revision_context(trust_origin="unknown"):
            pass


def test_bare_considered_without_an_act_is_impossible(store: Store) -> None:
    # No context value spells "considered": only a consider act does.
    with pytest.raises(psycopg.errors.CheckViolation, match="malformed trust act"):
        with store.pool.connection() as conn:
            conn.execute("SELECT set_config('precis.trust_origin', 'generated', true)")
            conn.execute(
                "SELECT set_config('precis.trust_act', "
                '\'{"action": "consider", "target_kind": "ref"}\', true)'
            )
            conn.execute("INSERT INTO refs (kind, title) VALUES ('memory', 'x')")


def test_unconverted_writer_cannot_change_stamped_content(store: Store) -> None:
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        mid = _memory(store)
    with pytest.raises(psycopg.errors.CheckViolation, match="version 1"):
        _set_title(store, mid, "rewritten without context")
    with pytest.raises(psycopg.errors.CheckViolation, match="body of ref"):
        store.chunks.replace_body_chunk(mid, "new body", chunk_kind="memory_body")
    ref = store.get_ref(kind="memory", id=mid)
    assert ref is not None and ref.title == "a memory"
    # bookkeeping keeps the stamp and needs no context
    store.update_ref(mid, meta_patch={"last_seen": "2026-10-15T00:00:00Z"})
    assert _stamp(store, "ref", mid) == ("generated", "hunch", 1)


def test_unconverted_writer_may_change_legacy_rows(store: Store) -> None:
    mid = _memory(store)
    _set_title(store, mid, "legacy edit")
    assert _stamp(store, "ref", mid) == ("unknown", "unknown", 0)


# ── Considered / demote / correct-origin / legacy-import ───────────────────


def test_considered_needs_an_explicit_act_and_survives_no_edit(store: Store) -> None:
    with revision_context(trust_origin="human", trust_actor_type="human"):
        mid = _memory(store)
    receipt = store.record_trust_act(
        _consider(store, "ref", mid), actor="user:reto", actor_type="human"
    )
    assert receipt.state.confidence == "considered" and not receipt.replayed
    assert receipt.act["before"]["confidence"] == "hunch"
    assert receipt.act["reason"] == "read it against the source"
    # a content change by a converted writer drops back to hunch
    with revision_context(trust_origin="human", trust_actor_type="human"):
        _set_title(store, mid, "edited after consideration")
    assert _stamp(store, "ref", mid) == ("human", "hunch", 1)
    actions = [a["action"] for a in store.trust_acts_for("ref", mid)]
    assert actions == ["create", "consider", "revise"]


def test_considered_on_create_binds_the_first_row_of_its_kind(store: Store) -> None:
    act = TrustAct(
        action="consider",
        target_kind="ref",
        create_match="memory",
        reason="written as a considered note",
    )
    with revision_context(
        trust_origin="human", trust_actor_type="human", trust_act=act
    ):
        with store.tx() as conn:
            first = store.insert_ref(
                kind="memory", slug=None, title="m1", meta={}, conn=conn
            )
            second = store.insert_ref(
                kind="memory", slug=None, title="m2", meta={}, conn=conn
            )
    assert _stamp(store, "ref", first.id) == ("human", "considered", 1)
    assert _stamp(store, "ref", second.id) == ("human", "hunch", 1)
    (rec,) = store.trust_acts_for("ref", first.id)
    assert rec["action"] == "consider" and rec["op_id"] == act.op_id


def test_legacy_consideration_keeps_unknown_origin(store: Store) -> None:
    mid = _memory(store)
    store.record_trust_act(
        _consider(store, "ref", mid), actor="reto", actor_type="human"
    )
    assert _stamp(store, "ref", mid) == ("unknown", "considered", 1)


def test_demotion_is_recorded(store: Store) -> None:
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        mid = _memory(store)
    store.record_trust_act(
        _consider(store, "ref", mid), actor="reto", actor_type="human"
    )
    sha = _sha(store, "ref", mid)
    demote = TrustAct(
        action="demote",
        target_kind="ref",
        target_id=mid,
        expected_sha=sha,
        reason="source retracted",
    )
    receipt = store.record_trust_act(demote, actor="reto", actor_type="human")
    assert receipt.state.confidence == "hunch"
    assert receipt.act["before"]["confidence"] == "considered"


def test_correct_origin_and_legacy_import(store: Store) -> None:
    mid = _memory(store)
    sha = _sha(store, "ref", mid)
    imported = store.record_trust_act(
        TrustAct(
            action="legacy-import",
            target_kind="ref",
            target_id=mid,
            expected_sha=sha,
            reason="mirror import of a legacy file",
        ),
        actor="op",
        actor_type="operator",
    )
    assert imported.state.version == 1 and imported.state.origin == "unknown"
    corrected = store.record_trust_act(
        TrustAct(
            action="correct-origin",
            target_kind="ref",
            target_id=mid,
            expected_sha=sha,
            origin="extracted",
            reason="it is a verbatim quote",
        ),
        actor="op",
        actor_type="operator",
    )
    assert (corrected.state.origin, corrected.state.confidence) == (
        "extracted",
        "unknown",
    )


def test_carry_names_its_predecessor(store: Store) -> None:
    with revision_context(trust_origin="human", trust_actor_type="human"):
        old = _memory(store)
    store.record_trust_act(
        _consider(store, "ref", old), actor="reto", actor_type="human"
    )
    carry = TrustAct(
        action="carry",
        target_kind="ref",
        create_match="memory",
        carry_from=old,
        reason="split into a new node, same assertion",
    )
    with revision_context(
        trust_origin="computed", trust_actor_type="worker", trust_act=carry
    ):
        new = _memory(store, title="successor")
    assert _stamp(store, "ref", new) == ("human", "considered", 1)
    (rec,) = store.trust_acts_for("ref", new)
    assert rec["action"] == "carry" and rec["payload"]["carry_from"] == old


def test_stale_expected_version_is_refused(store: Store) -> None:
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        mid = _memory(store)
    act = _consider(store, "ref", mid)
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        _set_title(store, mid, "changed under the reviewer")
    with pytest.raises(BadInput, match="stale act"):
        store.record_trust_act(act, actor="reto", actor_type="human")
    assert _stamp(store, "ref", mid) == ("generated", "hunch", 1)


def test_replay_is_idempotent_and_changed_payload_conflicts(store: Store) -> None:
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        mid = _memory(store)
    act = _consider(store, "ref", mid)
    first = store.record_trust_act(act, actor="reto", actor_type="human")
    again = store.record_trust_act(act, actor="reto", actor_type="human")
    assert again.replayed and again.act == first.act
    assert len(store.trust_acts_for("ref", mid)) == 2  # create + one consider
    changed = TrustAct(
        action="consider",
        target_kind="ref",
        target_id=mid,
        expected_sha=act.expected_sha,
        reason="a different reason",
        op_id=act.op_id,
    )
    with pytest.raises(BadInput, match="different payload"):
        store.record_trust_act(changed, actor="reto", actor_type="human")


def test_consider_on_link(store: Store) -> None:
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        a = _memory(store, title="a")
        b = _memory(store, title="b")
        lid = store.add_link(src_ref_id=a, dst_ref_id=b, relation="related-to").id
    assert _stamp(store, "link", lid) == ("generated", "hunch", 1)
    receipt = store.record_trust_act(
        _consider(store, "link", lid), actor="reto", actor_type="human"
    )
    assert receipt.state.confidence == "considered"
    with pytest.raises(psycopg.errors.CheckViolation, match="version 1"):
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE links SET relation = 'see-also' WHERE link_id = %s", (lid,)
            )


# ── Body/ref boundary ──────────────────────────────────────────────────────


def test_converted_body_replace_restamps_the_owner(store: Store) -> None:
    with revision_context(trust_origin="human", trust_actor_type="human"):
        mid = _memory(store)
    store.record_trust_act(
        _consider(store, "ref", mid), actor="reto", actor_type="human"
    )
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        store.chunks.replace_body_chunk(
            mid, "rewritten by a model", chunk_kind="memory_body"
        )
    assert _stamp(store, "ref", mid) == ("generated", "hunch", 1)
    last = store.trust_acts_for("ref", mid)[-1]
    assert (last["action"], last.get("via")) == ("revise", "body")
    assert last["before"]["confidence"] == "considered"


def test_body_move_checks_both_owners(store: Store) -> None:
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        stamped = _memory(store, title="stamped")
    legacy = _memory(store, title="legacy")
    chunk_id = store.chunks.list_chunks_for_ref(legacy)[0].id
    with pytest.raises(psycopg.errors.CheckViolation, match="body of ref"):
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE chunks SET ref_id = %s, ord = 5 WHERE chunk_id = %s",
                (stamped, chunk_id),
            )
    with revision_context(trust_origin="computed", trust_actor_type="worker"):
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE chunks SET ref_id = %s, ord = 5 WHERE chunk_id = %s",
                (stamped, chunk_id),
            )
    assert _stamp(store, "ref", stamped) == ("computed", "hunch", 1)
    assert _stamp(store, "ref", legacy) == ("computed", "hunch", 1)


def test_body_retire_by_raw_sql_cannot_leave_stale_considered(store: Store) -> None:
    with revision_context(trust_origin="human", trust_actor_type="human"):
        mid = _memory(store)
    store.record_trust_act(
        _consider(store, "ref", mid), actor="reto", actor_type="human"
    )
    with pytest.raises(psycopg.errors.CheckViolation, match="body of ref"):
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE chunks SET retired_at = now() WHERE ref_id = %s", (mid,)
            )
    assert _stamp(store, "ref", mid) == ("human", "considered", 1)


def test_derived_chunks_do_not_relabel_the_ref(store: Store) -> None:
    with revision_context(trust_origin="human", trust_actor_type="human"):
        mid = _memory(store)
    store.record_trust_act(
        _consider(store, "ref", mid), actor="reto", actor_type="human"
    )
    # a card/keyword chunk (ord < 0) is derived, not the ref's content
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
            "VALUES (%s, -1, 'card_combined', 'derived card')",
            (mid,),
        )
    assert _stamp(store, "ref", mid) == ("human", "considered", 1)


def test_timeline_entries_are_not_the_owner_subject(store: Store) -> None:
    """A worker appending a comment to an agent's gripe (diagnose_gripe, the
    executors) needs no context and does not relabel the gripe."""
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        gid = _memory(store)
    for ord_, kind in ((1, "gripe_comment"), (2, "job_summary"), (3, "run_log")):
        store.chunks.insert_chunks(
            gid, [ChunkInsert(ord=ord_, text="entry", meta={"chunk_kind": kind})]
        )
    assert _stamp(store, "ref", gid) == ("generated", "hunch", 1)
    with pytest.raises(psycopg.errors.CheckViolation, match="body of ref"):
        store.chunks.insert_chunks(
            gid,
            [ChunkInsert(ord=4, text="more body", meta={"chunk_kind": "memory_body"})],
        )


def test_content_sha_excludes_trust_and_reviews_stay_current(store: Store) -> None:
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        mid = _memory(store)
    sha = store.target_sha("ref", mid)
    store.record_target_review("ref", mid, actor="reto", verdict="approved")
    store.record_trust_act(
        _consider(store, "ref", mid), actor="reto", actor_type="human"
    )
    assert store.target_sha("ref", mid) == sha
    assert store.reviews_for("ref", mid)[0].current


# ── Transactions / acts / sealer ───────────────────────────────────────────


def test_same_xact_acts_keep_order_and_first_prior_state(store: Store) -> None:
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        mid = _memory(store)
    with store.tx() as conn, pin_connection(conn):
        with revision_context(trust_origin="generated", trust_actor_type="agent"):
            with store.pool.connection() as c:
                c.execute("UPDATE refs SET title = 'one' WHERE ref_id = %s", (mid,))
        sha = _one(conn, "SELECT precis_target_sha('ref', %s)", (mid,))
        act = TrustAct(
            action="consider",
            target_kind="ref",
            target_id=mid,
            expected_sha=sha,
            reason="checked the rewrite",
        )
        with revision_context(trust_act=act, trust_actor_type="human"):
            with store.pool.connection() as c:
                c.execute(
                    "UPDATE refs SET trust_origin = trust_origin WHERE ref_id = %s",
                    (mid,),
                )
    revs = store.revisions_for("ref", mid)
    create, rev = revs  # the creation's act row, then this transaction's
    assert [a["action"] for a in create.trust_acts] == ["create"]
    assert [a["action"] for a in rev.trust_acts] == ["revise", "consider"]
    assert rev.prev_state["title"] == "a memory"
    assert _stamp(store, "ref", mid) == ("generated", "considered", 1)


def test_nested_context_does_not_leak_in_a_pinned_transaction(store: Store) -> None:
    with store.tx() as conn, pin_connection(conn):
        with revision_context(trust_origin="extracted", trust_actor_type="worker"):
            with store.pool.connection() as c:
                inner = _one(
                    c,
                    "INSERT INTO refs (kind, title) VALUES ('memory', 'in') RETURNING ref_id",
                )
        with store.pool.connection() as c:
            outer = _one(
                c,
                "INSERT INTO refs (kind, title) VALUES ('memory', 'out') RETURNING ref_id",
            )
    assert _stamp(store, "ref", inner) == ("extracted", "hunch", 1)
    assert _stamp(store, "ref", outer) == ("unknown", "unknown", 0)


def test_pooled_reuse_is_isolated(store: Store) -> None:
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        _memory(store)
    # the next checkout outside any context sends nothing and sees nothing
    with store.pool.connection() as conn:
        value = _one(conn, "SELECT current_setting('precis.trust_origin', true)")
    assert (value or "") == ""
    assert _stamp(store, "ref", _memory(store)) == ("unknown", "unknown", 0)


def test_rollback_records_no_act(store: Store) -> None:
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        mid = _memory(store)
    act = _consider(store, "ref", mid)
    with pytest.raises(RuntimeError):
        with revision_context(trust_act=act, trust_actor_type="human"):
            with store.pool.connection() as conn:
                conn.execute(
                    "UPDATE refs SET trust_origin = trust_origin WHERE ref_id = %s",
                    (mid,),
                )
                raise RuntimeError("abort")
    assert [a["action"] for a in store.trust_acts_for("ref", mid)] == ["create"]


def test_noop_upsert_records_no_phantom_create(store: Store) -> None:
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        a = _memory(store, title="a")
        b = _memory(store, title="b")
        lid = store.add_link(src_ref_id=a, dst_ref_id=b, relation="related-to").id
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO links (src_ref_id, dst_ref_id, relation, set_by) "
                "SELECT src_ref_id, dst_ref_id, relation, set_by FROM links "
                " WHERE link_id = %s ON CONFLICT DO NOTHING",
                (lid,),
            )
    assert [a["action"] for a in store.trust_acts_for("link", lid)] == ["create"]


def test_edit_consider_undo_keeps_every_act(store: Store) -> None:
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        mid = _memory(store)
    with store.tx() as conn, pin_connection(conn):
        with revision_context(trust_origin="generated", trust_actor_type="agent"):
            with store.pool.connection() as c:
                c.execute("UPDATE refs SET title = 'tmp' WHERE ref_id = %s", (mid,))
        sha = _one(conn, "SELECT precis_target_sha('ref', %s)", (mid,))
        act = TrustAct(
            action="consider",
            target_kind="ref",
            target_id=mid,
            expected_sha=sha,
            reason="considered the interim wording",
        )
        with revision_context(trust_act=act, trust_actor_type="human"):
            with store.pool.connection() as c:
                c.execute(
                    "UPDATE refs SET trust_origin = trust_origin WHERE ref_id = %s",
                    (mid,),
                )
        with revision_context(trust_origin="generated", trust_actor_type="agent"):
            with store.pool.connection() as c:
                c.execute(
                    "UPDATE refs SET title = 'a memory' WHERE ref_id = %s", (mid,)
                )
    _create, undone = store.revisions_for("ref", mid)
    # content is back where it started, but the act row survives the sealer
    assert undone.new_sha == undone.prev_sha
    assert [a["action"] for a in undone.trust_acts] == ["revise", "consider", "revise"]
    assert _stamp(store, "ref", mid) == ("generated", "hunch", 1)


def test_empty_content_neutral_edit_keeps_old_noop(store: Store) -> None:
    mid = _memory(store)
    with revision_context("noop"):
        _set_title(store, mid, "tmp")
        _set_title(store, mid, "a memory")
    # legacy row, no trust acts: the 0185 sealer still drops back-to-start rows
    assert all(not r.trust_acts for r in store.revisions_for("ref", mid))


# ── Compatibility / cutover ────────────────────────────────────────────────


def test_compat_report_separates_window_writes(store: Store) -> None:
    _memory(store)
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        _memory(store)
    report = store.trust_compat_report()
    assert report["mode"] == "compat"
    refs = report["ref"]
    assert {"kind": "memory", "set_by": "(none)", "count": 1} in [
        {k: r[k] for k in ("kind", "set_by", "count")} for r in refs["compat_writes"]
    ]
    stamps = {(s["version"], s["origin"]): s["count"] for s in refs["stamps"]}
    assert stamps[(1, "generated")] == 1 and stamps[(0, "unknown")] == 1


def test_strict_mode_refuses_contextless_writes(store: Store) -> None:
    legacy = _memory(store)
    store.activate_trust_strict(lock_timeout="5s")
    try:
        with pytest.raises(psycopg.errors.CheckViolation, match="strict mode"):
            _memory(store)
        with pytest.raises(psycopg.errors.CheckViolation, match="strict mode"):
            _set_title(store, legacy, "late unconverted edit")
        # bookkeeping still passes; converted writers still write
        store.update_ref(legacy, meta_patch={"seen": True})
        with revision_context(trust_origin="computed", trust_actor_type="worker"):
            ok = _memory(store)
        assert _stamp(store, "ref", ok) == ("computed", "hunch", 1)
        # domain-only tables need no trust context
        with store.pool.connection() as conn:
            conn.execute("SELECT count(*) FROM tags")
    finally:
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE trust_write_state SET mode = 'compat', strict_at = NULL"
            )


def test_strict_switch_waits_for_inflight_writer_and_times_out(store: Store) -> None:
    assert store.dsn is not None
    holder = psycopg.connect(store.dsn)
    try:
        holder.execute("INSERT INTO refs (kind, title) VALUES ('memory', 'in flight')")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            store.activate_trust_strict(lock_timeout="200ms")
        assert store.trust_compat_report()["mode"] == "compat"
        holder.commit()
    finally:
        holder.close()


# ── operator CLI ───────────────────────────────────────────────────────────


def test_cli_report_show_and_consider(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from precis.cli import trust as cli

    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        mid = _memory(store)
    parser = argparse.ArgumentParser()
    cli.add_parser(parser.add_subparsers(dest="cmd"))

    def run(*argv: str) -> str:
        cli.run(parser.parse_args(["trust", *argv, "--dsn", str(store.dsn)]))
        return capsys.readouterr().out

    assert "mode compat" in run("report")
    sha = _sha(store, "ref", mid)
    out = run(
        "consider",
        "ref",
        str(mid),
        "--sha",
        str(sha),
        "--reason",
        "checked",
        "--by",
        "op",
    )
    assert "now generated · considered v1" in out
    shown = run("show", "ref", str(mid))
    assert "consider: generated/hunch -> generated/considered by op (operator)" in shown
