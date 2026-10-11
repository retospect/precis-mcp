"""Identity-preserving views and real PostgreSQL transaction races (dev only)."""

from __future__ import annotations

import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from psycopg import errors

from precis.design import history
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.conversation import ConversationHandler
from precis.handlers.pcb import PcbHandler
from precis.store import ChunkInsert
from precis_web import design_turn, pcb_design


def seed(store, slug="selection_fixture"):
    hub = Hub(store=store)
    PcbHandler(hub=hub).put(
        id=slug,
        args={
            "components": [
                {
                    "refdes": r,
                    "label": "resistor",
                    "footprint": "local-R",
                    "pins": [{"name": "1", "pad": "1"}],
                }
                for r in ("R1", "R2")
            ],
            "nets": [{"name": "SIG"}],
            "connections": [{"net": "SIG", "refdes": "R1", "pin": "1"}],
            "footprints": [
                {
                    "name": "local-R",
                    "pads": [
                        {
                            "number": "1",
                            "x": 0,
                            "y": 0,
                            "w": 0.6,
                            "h": 0.6,
                            "shape": "rect",
                        }
                    ],
                }
            ],
        },
    )
    ref = store.get_ref(kind="pcb", id=slug)
    with store.pcb_view_tx() as conn:
        view = pcb_design.capture_view(store, ref, conn=conn)
    context = {**view.context, "selected": [view.instances[0]]}
    return hub, ref, context


def proposal(hub, ref, context, fixed="xy"):
    return design_turn.run_turn(
        hub,
        kind="pcb",
        slug=ref.slug,
        message="lock resistor",
        context=context,
        model_call=lambda _: json.dumps(
            {
                "ops": [{"op": "move", "refdes": "R1", "fixed": fixed}],
                "rationale": "hold position",
            }
        ),
    )


def test_view_distinguishes_instances_sharing_a_footprint_and_get_writes_nothing(store):
    _, ref, context = seed(store)
    with store.pcb_view_tx() as conn:
        view = pcb_design.capture_view(store, ref, conn=conn)
        assert conn.execute("SHOW transaction_read_only").fetchone()[0] == "on"
    assert len({i["instance_id"] for i in view.instances}) == 2
    assert view.instances[0]["footprint"] == view.instances[1]["footprint"]
    assert view.instances[0]["footprint"]["id"] == {"ref_id": ref.id, "name": "local-R"}
    assert pcb_design.resolve_selection(view, context) == context
    assert history.list_checkpoints(store, ref.id) == []
    assert history.list_revisions(store, ref.id) == []
    forged = copy.deepcopy(context)
    forged["selected"][0]["footprint"]["id"]["ref_id"] += 1
    with pytest.raises(ValueError, match="forged"):
        pcb_design.resolve_selection(view, forged)


def test_proposal_requires_explicit_canonical_apply_and_atomic_history(store):
    hub, ref, context = seed(store)
    before = store.pcb_load(ref.id)
    turn = proposal(hub, ref, context)
    assert not turn.applied and turn.valid and turn.proposal
    assert store.pcb_load(ref.id) == before
    assert history.list_checkpoints(store, ref.id) == []
    assert turn.turn == "pcb-design-chat-selection_fixture~0"
    denied = design_turn.apply_proposal(
        hub,
        kind="pcb",
        slug=ref.slug,
        turn=turn.turn,
        shown=context["shown"]["snapshot"],
        ops=[{"op": "move", "refdes": "R2", "fixed": "both"}],
    )
    assert not denied.applied
    applied = design_turn.apply_proposal(
        hub,
        kind="pcb",
        slug=ref.slug,
        turn=turn.turn,
        shown=context["shown"]["snapshot"],
    )
    assert applied.applied, applied.error
    assert applied.revision == 1
    assert [r.turn for r in history.list_revisions(store, ref.id)] == [turn.turn]
    assert len(history.list_checkpoints(store, ref.id)) == 2
    again = design_turn.apply_proposal(
        hub,
        kind="pcb",
        slug=ref.slug,
        turn=turn.turn,
        shown=context["shown"]["snapshot"],
    )
    assert not again.applied and "already applied" in (again.error or "")
    assert len(history.list_revisions(store, ref.id)) == 1


@pytest.mark.parametrize(
    "op",
    [
        {"op": "route"},
        {"op": "move", "refdes": "R2", "fixed": "xy"},
        {"op": "move", "refdes": "R1", "layer": "bottom"},
        {"op": "move", "refdes": "R1", "x": True},
        {"op": "move", "refdes": "R1", "x": float("nan")},
        {"op": "move", "refdes": "R1", "fixed": "all"},
        {"op": "move", "refdes": "R1"},
    ],
)
def test_strict_initial_move_roster(store, op):
    _, _, context = seed(store)
    assert pcb_design.vet_move([op], context)


def test_model_mutation_after_display_is_refused_without_history(store):
    hub, ref, context = seed(store)

    def model(_):
        store.pcb_move_instance(ref.id, "R1", fixed="rot")
        return json.dumps(
            {
                "ops": [{"op": "move", "refdes": "R1", "fixed": "xy"}],
                "rationale": "lock",
            }
        )

    with pytest.raises(ValueError, match="changed"):
        design_turn.run_turn(
            hub,
            kind="pcb",
            slug=ref.slug,
            message="lock",
            context=context,
            model_call=model,
        )
    assert history.list_revisions(store, ref.id) == []
    assert history.list_checkpoints(store, ref.id) == []
    assert (
        store.get_ref(kind="conv", id=design_turn.conv_slug(ref.slug, kind="pcb"))
        is None
    )


def test_cross_owner_instance_is_refused_including_reverse_dependency(store):
    _, ref, _ = seed(store)
    _, foreign, _ = seed(store, "other_fixture")
    with store.tx() as conn:
        conn.execute(
            "UPDATE pcb_instances SET component_id=(SELECT component_id FROM pcb_components WHERE ref_id=%s LIMIT 1) WHERE ref_id=%s AND refdes='R2'",
            (foreign.id, ref.id),
        )
    with store.pcb_view_tx() as conn, pytest.raises(ValueError, match="cross-owner"):
        pcb_design.capture_view(store, ref, conn=conn)


def test_shared_append_serializes_first_slug_race_and_returns_actual_stored_turn(store):
    barrier = threading.Barrier(2)

    def append(n):
        barrier.wait(timeout=5)
        return ConversationHandler(hub=Hub(store=store)).append_turn(
            slug="absent-conv", body=f"message{n}", author="fixture", msg_id=str(n)
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(append, (0, 1)))
    assert {r.chunk.ord for r in rows} == {0, 1}
    assert len({r.ref.id for r in rows}) == 1
    assert sum(r.created for r in rows) == 1
    assert {r.turn for r in rows} == {"absent-conv~0", "absent-conv~1"}
    # A hole/synthetic card must not turn count(*) into a colliding ordinal.
    store.chunks.insert_chunks(rows[0].ref.id, [ChunkInsert(ord=7, text="hole")])
    result = ConversationHandler(hub=Hub(store=store)).append_turn(
        slug="absent-conv", body="next", author="fixture"
    )
    assert result.chunk.ord == 8


def test_shared_append_duplicate_race_captures_once(store):
    barrier = threading.Barrier(2)

    def append(_):
        barrier.wait(timeout=5)
        return ConversationHandler(hub=Hub(store=store)).append_turn(
            slug="duplicate-conv", body="same", author="fixture", msg_id="same-id"
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(append, (0, 1)))
    assert rows[0].chunk.id == rows[1].chunk.id
    assert sum(r.duplicate for r in rows) == 1


def test_simultaneous_duplicate_apply_commits_one_revision(store):
    hub, ref, context = seed(store)
    turn = proposal(hub, ref, context)
    barrier = threading.Barrier(2)

    def apply(_):
        barrier.wait(timeout=5)
        return design_turn.apply_proposal(
            hub,
            kind="pcb",
            slug=ref.slug,
            turn=turn.turn,
            shown=context["shown"]["snapshot"],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(apply, (0, 1)))
    assert sum(r.applied for r in rows) == 1, rows
    assert len(history.list_revisions(store, ref.id)) == 1
    assert len(history.list_checkpoints(store, ref.id)) == 2


def _wait_blocked(store, pid):
    deadline = time.monotonic() + 1.0
    with store.pool.connection() as observer:
        while time.monotonic() < deadline:
            if observer.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE %s=ANY(pg_blocking_pids(pid)))",
                (pid,),
            ).fetchone()[0]:
                return
            time.sleep(0.005)
    raise AssertionError("guard did not wait on the expected writer")


def test_child_committing_while_guard_waits_is_in_captured_state(store):
    hub, ref, context = seed(store)
    ready = threading.Event()

    def guarded():
        ready.set()
        with store.pcb_guarded_tx(ref.id, ref.slug) as conn:
            return pcb_design.capture_view(store, ref, conn=conn)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.tx() as writer:
            pid = writer.info.backend_pid
            writer.execute(
                "SELECT ref_id FROM refs WHERE ref_id=%s FOR UPDATE", (ref.id,)
            )
            future = pool.submit(guarded)
            assert ready.wait(1)
            _wait_blocked(store, pid)
            writer.execute(
                "INSERT INTO pcb_nets(ref_id,name) VALUES (%s,'arriving-child')",
                (ref.id,),
            )
        view = future.result(timeout=3)
    assert "arriving-child" in {n["name"] for n in view.raw["tables"]["pcb_nets"]}
    with pytest.raises(ValueError, match="changed"):
        pcb_design.resolve_selection(view, context)
    assert history.list_revisions(store, ref.id) == []


def test_cache_guard_protects_absent_keys_allows_reads_and_bounds_contention(store):
    _, ref, _ = seed(store)
    elapsed: dict[str, float] = {}

    def cache_write():
        start = time.monotonic()
        with store.tx() as writer:
            writer.execute("SET LOCAL lock_timeout='100ms'")
            with pytest.raises(errors.LockNotAvailable):
                writer.execute("INSERT INTO part_footprints(lcsc) VALUES ('C999999')")
        elapsed["write"] = time.monotonic() - start

    start = time.monotonic()
    with store.pcb_guarded_tx(ref.id, ref.slug):
        with store.pool.connection() as reader:
            assert reader.execute("SELECT count(*) FROM parts").fetchone()[0] == 0
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(cache_write).result(timeout=1)
    elapsed["guard"] = time.monotonic() - start
    assert 0.08 <= elapsed["write"] < 1.0
    assert elapsed["guard"] < 2.0
    from pathlib import Path

    evidence = Path(".scratch/selection.local/measured-cache-contention.json")
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        json.dumps(
            {
                "upper_bound_seconds": elapsed,
                "lock_timeout_ms": 100,
                "guard_deadline_seconds": 2.0,
                "unrelated_reads_pass": True,
                "absent_insert_refused": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    print("PCB cache contention measured seconds:", elapsed)
    with store.pool.connection() as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM part_footprints WHERE lcsc='C999999'"
            ).fetchone()
            is None
        )


def test_cache_writer_makes_guard_refuse_nowait_without_partial_history(store):
    hub, ref, context = seed(store)
    turn = proposal(hub, ref, context)
    with store.tx() as writer:
        writer.execute("INSERT INTO part_footprints(lcsc) VALUES ('C999999')")
        result = design_turn.apply_proposal(
            hub,
            kind="pcb",
            slug=ref.slug,
            turn=turn.turn,
            shown=context["shown"]["snapshot"],
        )
        assert not result.applied
        assert "lock" in (result.error or "").lower()
    assert history.list_revisions(store, ref.id) == []
    assert history.list_checkpoints(store, ref.id) == []
    assert store.pcb_load(ref.id)["instances"][0]["fixed"] is None


def test_existing_child_update_committing_during_leaf_wait_changes_token(store):
    _, ref, context = seed(store)
    with store.pool.connection() as reader:
        pin = reader.execute(
            "SELECT pin_id FROM pcb_pins p JOIN pcb_components c USING(component_id) WHERE c.ref_id=%s ORDER BY pin_id LIMIT 1",
            (ref.id,),
        ).fetchone()[0]

    def guarded():
        with store.pcb_guarded_tx(ref.id, ref.slug) as conn:
            return pcb_design.capture_view(store, ref, conn=conn)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.tx() as writer:
            writer.execute(
                "SELECT pin_id FROM pcb_pins WHERE pin_id=%s FOR UPDATE", (pin,)
            )
            future = pool.submit(guarded)
            _wait_blocked(store, writer.info.backend_pid)
            writer.execute(
                "UPDATE pcb_pins SET note='late-committed' WHERE pin_id=%s", (pin,)
            )
        view = future.result(timeout=3)
    assert view.token != context["shown"]["snapshot"]


def test_slug_delete_rebind_while_guard_waits_refuses_old_numeric_board(store):
    _, ref, _ = seed(store)
    _, foreign, _ = seed(store, "foreign-selection")

    def guarded():
        with store.pcb_guarded_tx(ref.id, ref.slug):
            raise AssertionError("rebound slug must refuse before yield")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.tx() as writer:
            writer.execute(
                "SELECT ref_id FROM refs WHERE ref_id=%s FOR UPDATE", (ref.id,)
            )
            future = pool.submit(guarded)
            _wait_blocked(store, writer.info.backend_pid)
            writer.execute(
                "DELETE FROM ref_identifiers WHERE id_kind='cite_key' AND id_value=%s",
                (ref.slug,),
            )
            writer.execute(
                "INSERT INTO ref_identifiers(id_kind,id_value,ref_id) VALUES ('cite_key',%s,%s)",
                (ref.slug, foreign.id),
            )
        with pytest.raises(ValueError, match="binding changed"):
            future.result(timeout=3)
    assert history.list_checkpoints(store, ref.id) == []


def test_catalogue_replacement_is_blocked_but_unrelated_cache_reads_continue(store):
    _, ref, _ = seed(store)

    def replacing():
        with store.tx() as writer:
            writer.execute("SET LOCAL lock_timeout='100ms'")
            with pytest.raises(errors.LockNotAvailable):
                writer.execute("ALTER TABLE parts RENAME TO parts_fixture_replacement")

    with store.pcb_guarded_tx(ref.id, ref.slug):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(replacing).result(timeout=1)
        with store.pool.connection() as reader:
            assert (
                reader.execute("SELECT count(*) FROM part_footprints").fetchone()[0]
                == 0
            )


def test_retired_rows_are_locked_against_revival(store):
    _, ref, _ = seed(store)
    with store.tx() as conn:
        conn.execute(
            "UPDATE pcb_instances SET retired_at=now() WHERE ref_id=%s AND refdes='R2'",
            (ref.id,),
        )

    def revive():
        with store.tx() as writer:
            writer.execute("SET LOCAL lock_timeout='100ms'")
            with pytest.raises(errors.LockNotAvailable):
                writer.execute(
                    "UPDATE pcb_instances SET retired_at=NULL WHERE ref_id=%s AND refdes='R2'",
                    (ref.id,),
                )

    with store.pcb_guarded_tx(ref.id, ref.slug):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(revive).result(timeout=1)


def test_proposal_arrival_during_apply_serializes_without_corrupting_history(
    store, monkeypatch
):
    hub, ref, context = seed(store)
    turn = proposal(hub, ref, context)
    entered = threading.Event()
    release = threading.Event()
    original_move = pcb_design._move

    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=1)
        return original_move(*args, **kwargs)

    monkeypatch.setattr(pcb_design, "_move", paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        apply = pool.submit(
            design_turn.apply_proposal,
            hub,
            kind="pcb",
            slug=ref.slug,
            turn=turn.turn,
            shown=context["shown"]["snapshot"],
        )
        assert entered.wait(timeout=1)
        cslug = design_turn.conv_slug(ref.slug, kind="pcb")
        # An ordinary transcript writer must wait for the guarded ordered refs.
        append = pool.submit(
            ConversationHandler(hub=hub).append_turn,
            slug=cslug,
            body="another proposal",
            author="fixture",
            related_ref_id=ref.id,
            meta={
                "design_kind": "pcb",
                "design": ref.slug,
                "outcome": "proposal (1 op(s), valid)",
                "ops": [{"op": "move", "refdes": "R1", "fixed": "both"}],
                "context": context,
            },
        )
        # Observe an actual blocked backend, not a sleep-based race assumption.
        with store.pool.connection() as observer:
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                if observer.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid))>0)"
                ).fetchone()[0]:
                    break
                time.sleep(0.005)
            else:
                raise AssertionError("append did not block behind Apply")
        release.set()
        applied = apply.result(timeout=3)
        appended = append.result(timeout=3)
    assert applied.applied, applied.error
    assert appended.chunk.ord == 1
    assert [r.turn for r in history.list_revisions(store, ref.id)] == [turn.turn]
    assert len(design_turn.transcript(store, ref.slug, kind="pcb")) == 2


def test_remove_recreate_refdes_refuses_old_instance_selection(store):
    hub, ref, context = seed(store)
    turn = proposal(hub, ref, context)
    with store.tx() as conn:
        conn.execute(
            "UPDATE pcb_instances SET retired_at=now() WHERE instance_id=%s",
            (context["selected"][0]["instance_id"],),
        )
        conn.execute(
            "INSERT INTO pcb_instances(ref_id,board_id,component_id,refdes) SELECT ref_id,board_id,component_id,refdes FROM pcb_instances WHERE instance_id=%s",
            (context["selected"][0]["instance_id"],),
        )
    result = design_turn.apply_proposal(
        hub,
        kind="pcb",
        slug=ref.slug,
        turn=turn.turn,
        shown=context["shown"]["snapshot"],
    )
    assert not result.applied
    assert history.list_revisions(store, ref.id) == []
    assert history.list_checkpoints(store, ref.id) == []


def test_foreign_conversation_namespace_is_not_appended(store):
    hub, ref, context = seed(store)
    cslug = design_turn.conv_slug(ref.slug, kind="pcb")
    store.insert_ref(
        kind="conv",
        slug=cslug,
        title="foreign",
        meta={"design_kind": "se", "design": ref.slug},
    )
    with pytest.raises(BadInput, match="ownership"):
        proposal(hub, ref, context)
    conv = store.get_ref(kind="conv", id=cslug)
    assert store.chunks.list_chunks_for_ref(conv.id) == []


def test_default_renderer_bytes_unchanged_and_manifest_targets_escape_identity(store):
    hub, ref, _ = seed(store)
    store.pcb_move_instance(ref.id, "R1", x=10, y=10, rot=0)
    store.pcb_move_instance(ref.id, "R2", x=20, y=10, rot=0)
    with store.pcb_view_tx() as conn:
        view = pcb_design.capture_view(store, ref, conn=conn)
        plain = PcbHandler(hub=hub)._render_fab_svg(ref.id, ref.slug).body
        marked = (
            PcbHandler(hub=hub)
            ._render_fab_svg(
                ref.id,
                ref.slug,
                selection_manifest={
                    "board_ref_id": ref.id,
                    "snapshot": view.token,
                    "instances": view.instances,
                },
            )
            .body
        )
        assert PcbHandler(hub=hub)._render_fab_svg(ref.id, ref.slug).body == plain
    assert "data-pcb-instance" not in plain
    assert f'data-pcb-board="{ref.id}"' in marked
    for i in view.instances:
        assert f'data-pcb-instance="{i["instance_id"]}"' in marked
    import xml.etree.ElementTree as ET

    ET.fromstring(marked)


def test_geometry_move_and_no_collateral_rollback(store):
    hub, ref, context = seed(store)
    # Exact authored local geometry, not a catalogue placeholder.
    store.pcb_move_instance(ref.id, "R1", x=10, y=10, rot=0)
    store.pcb_move_instance(ref.id, "R2", x=20, y=10, rot=0)
    with store.pcb_view_tx() as conn:
        view = pcb_design.capture_view(store, ref, conn=conn)
    context = {**view.context, "selected": [view.instances[0]]}
    before = copy.deepcopy(view.raw)
    error = pcb_design.dry_run(
        hub, ref, context, [{"op": "move", "refdes": "R1", "x": 11.0}]
    )
    assert error is None, error
    with store.pcb_view_tx() as conn:
        after = pcb_design.capture_view(store, ref, conn=conn)
    assert pcb_design._stable(after.raw) == pcb_design._stable(before)
    assert history.list_checkpoints(store, ref.id) == []


@pytest.mark.parametrize("legacy_kind", ["se", "structure"])
@pytest.mark.parametrize("pcb_first", [False, True])
def test_pcb_and_legacy_namespaces_stay_separate_in_both_creation_orders(
    store, legacy_kind, pcb_first
):
    hub, ref, context = seed(store, "foo")
    legacy = store.insert_ref(kind=legacy_kind, slug="pcb-foo", title="legacy")

    def append_legacy(message):
        return design_turn._write_transcript(
            hub,
            kind=legacy_kind,
            design_ref=legacy,
            turn=f"design-chat-pcb-foo~{message}",
            message=str(message),
            handles=[],
            rationale="legacy",
            ops=[{"op": "legacy-fixture"}],
            outcome="proposal (1 op(s), valid)",
        )

    if pcb_first:
        pending = proposal(hub, ref, context)
        append_legacy(0)
    else:
        append_legacy(0)
        pending = proposal(hub, ref, context)
    append_legacy(1)
    assert design_turn.conv_slug("pcb-foo", kind=legacy_kind) == "design-chat-pcb-foo"
    assert pending.turn == "pcb-design-chat-foo~0"
    assert [
        t.message for t in design_turn.transcript(store, "pcb-foo", kind=legacy_kind)
    ] == ["0", "1"]
    assert [t.message for t in design_turn.transcript(store, "foo", kind="pcb")] == [
        "lock resistor"
    ]
    legacy_pending = design_turn.pending_proposal(
        store, kind=legacy_kind, slug="pcb-foo"
    )
    pcb_pending = design_turn.pending_proposal(store, kind="pcb", slug="foo")
    assert legacy_pending is not None and pcb_pending is not None
    assert legacy_pending.handle == "design-chat-pcb-foo~1"
    assert pcb_pending.handle == pending.turn
    with store.tx() as conn:
        links = conn.execute(
            "SELECT src_ref_id,dst_ref_id FROM links WHERE dst_ref_id=ANY(%s) AND relation='related-to' ORDER BY dst_ref_id",
            ([ref.id, legacy.id],),
        ).fetchall()
        assert len(links) == 2 and len({a for a, _ in links}) == 2
    assert design_turn.apply_proposal(
        hub,
        kind="pcb",
        slug="foo",
        turn=pending.turn,
        shown=context["shown"]["snapshot"],
    ).applied
    assert len(history.list_revisions(store, ref.id)) == 1
    assert len(design_turn.transcript(store, "pcb-foo", kind=legacy_kind)) == 2


@pytest.mark.parametrize("legacy_kind", ["se", "structure"])
def test_legacy_read_append_and_apply_refuse_previous_pcb_collision(
    store, legacy_kind, monkeypatch
):
    hub, pcb_ref, _ = seed(store, "foo")
    parent = store.insert_ref(kind=legacy_kind, slug="pcb-foo", title="legacy")
    conv = store.insert_ref(
        kind="conv",
        slug="design-chat-pcb-foo",
        title="foreign old PCB",
        meta={"design_kind": "pcb", "design": "foo"},
    )
    store.add_link(src_ref_id=conv.id, dst_ref_id=pcb_ref.id, relation="related-to")
    store.chunks.insert_chunks(
        conv.id,
        [
            ChunkInsert(
                ord=0,
                text="foreign PCB proposal",
                meta={
                    "design_kind": "pcb",
                    "design": "foo",
                    "ops": [{"op": "move", "refdes": "R1", "fixed": "xy"}],
                    "outcome": "proposal (1 op(s), valid)",
                },
            )
        ],
    )
    before = store.chunks.list_chunks_for_ref(conv.id)
    assert design_turn.transcript(store, "pcb-foo", kind=legacy_kind) == []
    assert design_turn.pending_proposal(store, kind=legacy_kind, slug="pcb-foo") is None
    with pytest.raises(BadInput, match="ownership"):
        design_turn._write_transcript(
            hub,
            kind=legacy_kind,
            design_ref=parent,
            turn="design-chat-pcb-foo~1",
            message="refuse",
            handles=[],
            rationale="legacy",
            ops=[],
            outcome="no op",
        )
    monkeypatch.setattr(
        design_turn,
        "build_digest",
        lambda *a, **kw: pytest.fail(
            "foreign conversation must refuse before model/digest"
        ),
    )
    with pytest.raises(BadInput, match="ownership"):
        design_turn.run_turn(hub, kind=legacy_kind, slug="pcb-foo", message="refuse")
    result = design_turn.apply_proposal(
        hub,
        kind=legacy_kind,
        slug="pcb-foo",
        turn="design-chat-pcb-foo~0",
        ops=[{"op": "set_desc", "block": "anything", "desc": "foreign"}],
    )
    assert not result.applied and "ownership" in (result.error or "")
    assert store.chunks.list_chunks_for_ref(conv.id) == before
    with store.tx() as conn:
        assert conn.execute(
            "SELECT dst_ref_id FROM links WHERE src_ref_id=%s AND relation='related-to'",
            (conv.id,),
        ).fetchall() == [(pcb_ref.id,)]
    assert history.list_revisions(store, parent.id) == []


@pytest.mark.parametrize("legacy_kind", ["se", "structure"])
def test_linked_metadata_free_legacy_transcript_keeps_identity(store, legacy_kind):
    hub = Hub(store=store)
    parent = store.insert_ref(kind=legacy_kind, slug="pcb-old", title="old legacy")
    cslug = "design-chat-pcb-old"
    conv = store.insert_ref(kind="conv", slug=cslug, title="old transcript")
    store.add_link(src_ref_id=conv.id, dst_ref_id=parent.id, relation="related-to")
    store.chunks.insert_chunks(
        conv.id,
        [
            ChunkInsert(
                ord=0,
                text="old proposal",
                meta={
                    "message": "old",
                    "ops": [{"op": "legacy-fixture"}],
                    "outcome": "proposal (1 op(s), valid)",
                },
            )
        ],
    )
    pending = design_turn.pending_proposal(store, kind=legacy_kind, slug="pcb-old")
    assert pending is not None and pending.handle == cslug + "~0"
    design_turn._write_transcript(
        hub,
        kind=legacy_kind,
        design_ref=parent,
        turn=cslug + "~1",
        message="new",
        handles=[],
        rationale="legacy",
        ops=[],
        outcome="no op",
    )
    assert [
        t.message for t in design_turn.transcript(store, "pcb-old", kind=legacy_kind)
    ] == ["old", "new"]
    assert store.get_ref(kind="conv", id=cslug).id == conv.id
    refused = design_turn.apply_proposal(
        hub,
        kind=legacy_kind,
        slug="pcb-old",
        turn="pcb-design-chat-other~0",
        ops=[{"op": "legacy-fixture"}],
    )
    assert not refused.applied and "ownership" in (refused.error or "")
    assert history.list_revisions(store, parent.id) == []


def test_unrelated_pcb_conv_is_hidden_and_refused_before_model_and_apply(store):
    hub, ref, context = seed(store)
    cslug = design_turn.conv_slug(ref.slug, kind="pcb")
    conv = store.insert_ref(kind="conv", slug=cslug, title="unrelated")
    store.chunks.insert_chunks(
        conv.id,
        [
            ChunkInsert(
                ord=0, text="foreign", meta={"outcome": "proposal (valid)", "ops": []}
            )
        ],
    )
    assert design_turn.transcript(store, ref.slug, kind="pcb") == []
    assert design_turn.pending_proposal(store, kind="pcb", slug=ref.slug) is None
    with pytest.raises(BadInput, match="ownership"):
        proposal(hub, ref, context)
    result = design_turn.apply_proposal(
        hub,
        kind="pcb",
        slug=ref.slug,
        turn=f"{cslug}~0",
        shown=context["shown"]["snapshot"],
    )
    assert not result.applied and "ownership" in (result.error or "")
    assert len(store.chunks.list_chunks_for_ref(conv.id)) == 1
    assert history.list_revisions(store, ref.id) == []


def test_unavailable_footprint_does_not_invent_identity_and_refuses_geometry(store):
    hub, ref, _ = seed(store)
    with store.tx() as conn:
        conn.execute(
            "UPDATE pcb_components SET part_lcsc='C999998' WHERE ref_id=%s", (ref.id,)
        )
    with store.pcb_view_tx() as conn:
        view = pcb_design.capture_view(store, ref, conn=conn)
    item = view.instances[0]
    assert item["footprint"]["id"] is None and not item["footprint"]["available"]
    context = {**view.context, "selected": [item]}
    error = pcb_design.dry_run(
        hub, ref, context, [{"op": "move", "refdes": "R1", "x": 10}]
    )
    assert "trusted footprint" in (error or "")
    assert history.list_checkpoints(store, ref.id) == []


def test_cached_library_uuid_and_volatile_stock_are_separate_from_instance_id(store):
    from psycopg.types.json import Jsonb

    _, ref, _ = seed(store)
    uuid = "0123456789abcdef0123456789abcdef"
    with store.tx() as conn:
        conn.execute(
            "UPDATE pcb_components SET part_lcsc='C999998' WHERE ref_id=%s", (ref.id,)
        )
        conn.execute(
            "INSERT INTO part_footprints(lcsc,source,pads) VALUES (%s,%s,%s)",
            (
                "C999998",
                "easyeda:packageDetail:" + uuid,
                Jsonb(
                    [
                        {
                            "number": "1",
                            "x": 0,
                            "y": 0,
                            "w": 0.6,
                            "h": 0.6,
                            "shape": "rect",
                        }
                    ]
                ),
            ),
        )
        conn.execute("INSERT INTO parts(lcsc,stock,price) VALUES ('C999998',100,'{}')")
    with store.pcb_view_tx() as conn:
        before = pcb_design.capture_view(store, ref, conn=conn)
    assert before.instances[0]["footprint"]["id"] == uuid
    assert before.instances[0]["footprint"]["part_lcsc"] == "C999998"
    with store.tx() as conn:
        conn.execute("UPDATE parts SET stock=5,price='{}',refreshed_at=now()")
    with store.pcb_view_tx() as conn:
        after = pcb_design.capture_view(store, ref, conn=conn)
    assert before.token == after.token


def test_repair_rejected_and_noop_use_shared_transcript_without_applying(store):
    hub, ref, context = seed(store)
    replies = iter(
        [
            "not JSON",
            json.dumps(
                {
                    "ops": [{"op": "move", "refdes": "R1", "fixed": "xy"}],
                    "rationale": "lock",
                }
            ),
        ]
    )
    repaired = design_turn.run_turn(
        hub,
        kind="pcb",
        slug=ref.slug,
        message="lock",
        context=context,
        model_call=lambda _: next(replies),
    )
    assert repaired.valid and repaired.repair and repaired.turn
    assert history.list_revisions(store, ref.id) == []
    noops = design_turn.run_turn(
        hub,
        kind="pcb",
        slug=ref.slug,
        message="question",
        context=context,
        model_call=lambda _: json.dumps(
            {"ops": [], "rationale": "cannot be expressed as ops"}
        ),
    )
    assert not noops.applied and not noops.ops and noops.turn
    rejected = design_turn.run_turn(
        hub,
        kind="pcb",
        slug=ref.slug,
        message="route",
        context=context,
        model_call=lambda _: json.dumps(
            {"ops": [{"op": "route"}], "rationale": "unknown"}
        ),
    )
    assert rejected.error and rejected.turn
    assert [r.ord for r in design_turn.transcript(store, ref.slug, kind="pcb")] == [
        0,
        1,
        2,
    ]
    pending = design_turn.pending_proposal(store, kind="pcb", slug=ref.slug)
    assert pending is not None and pending.handle == repaired.turn
    assert not design_turn.apply_proposal(
        hub,
        kind="pcb",
        slug=ref.slug,
        turn=rejected.turn,
        shown=context["shown"]["snapshot"],
    ).applied


def test_apply_superseded_and_changed_owner_link_refuse_without_partial_history(store):
    hub, ref, context = seed(store)
    first = proposal(hub, ref, context)
    second = proposal(hub, ref, context, fixed="both")
    refused = design_turn.apply_proposal(
        hub,
        kind="pcb",
        slug=ref.slug,
        turn=first.turn,
        shown=context["shown"]["snapshot"],
    )
    assert not refused.applied and "superseded" in (refused.error or "")
    _, foreign, _ = seed(store, "foreign-owner")
    conv = store.get_ref(kind="conv", id=design_turn.conv_slug(ref.slug, kind="pcb"))
    with store.tx() as conn:
        conn.execute(
            "UPDATE links SET dst_ref_id=%s WHERE src_ref_id=%s AND relation='related-to'",
            (foreign.id, conv.id),
        )
    denied = design_turn.apply_proposal(
        hub,
        kind="pcb",
        slug=ref.slug,
        turn=second.turn,
        shown=context["shown"]["snapshot"],
    )
    assert not denied.applied and "ownership" in (denied.error or "")
    assert history.list_revisions(store, ref.id) == []
    assert history.list_checkpoints(store, ref.id) == []


def test_invalid_context_refuses_before_any_model_call(store):
    hub, ref, context = seed(store)
    for invalid in (
        None,
        {},
        {**context, "selected": []},
        {**context, "selected": [context["selected"][0]] * 2},
    ):
        with pytest.raises(ValueError):
            design_turn.run_turn(
                hub,
                kind="pcb",
                slug=ref.slug,
                message="lock",
                context=invalid,
                model_call=lambda _: pytest.fail("model must not see invalid context"),
            )
    assert (
        store.get_ref(kind="conv", id=design_turn.conv_slug(ref.slug, kind="pcb"))
        is None
    )


def test_guard_deadline_rolls_back_move_checkpoints_and_consumption(store, monkeypatch):
    hub, ref, context = seed(store)
    turn = proposal(hub, ref, context)
    original = history.record_revision

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        time.sleep(2.05)
        return result

    monkeypatch.setattr(history, "record_revision", delayed)
    result = design_turn.apply_proposal(
        hub,
        kind="pcb",
        slug=ref.slug,
        turn=turn.turn,
        shown=context["shown"]["snapshot"],
    )
    assert not result.applied
    assert history.list_revisions(store, ref.id) == []
    assert history.list_checkpoints(store, ref.id) == []
    assert store.pcb_load(ref.id)["instances"][0]["fixed"] is None


def test_prompt_digest_and_selection_are_the_same_shown_snapshot(store, monkeypatch):
    hub, ref, context = seed(store)
    original = pcb_design.view_context

    def changed_between_reads(*args):
        canonical = original(*args)
        store.pcb_move_instance(ref.id, "R1", fixed="rot")
        return canonical

    monkeypatch.setattr(pcb_design, "view_context", changed_between_reads)
    with pytest.raises(ValueError, match="changed"):
        design_turn.run_turn(
            hub,
            kind="pcb",
            slug=ref.slug,
            message="lock",
            context=context,
            model_call=lambda _: pytest.fail(
                "model must not receive a mixed shown/digest snapshot"
            ),
        )
    assert history.list_revisions(store, ref.id) == []


@pytest.mark.parametrize("collateral", ["group", "router"])
def test_real_handler_group_and_router_collateral_are_refused_and_rolled_back(
    store, collateral
):
    from psycopg.types.json import Jsonb

    hub, ref, _ = seed(store)
    store.pcb_move_instance(ref.id, "R1", x=10, y=10, rot=0)
    store.pcb_move_instance(ref.id, "R2", x=20, y=10, rot=0)
    with store.tx() as conn:
        if collateral == "group":
            conn.execute(
                "UPDATE pcb_instances SET refdes='G_R2' WHERE ref_id=%s AND refdes='R2'",
                (ref.id,),
            )
        conn.execute(
            "INSERT INTO pcb_generators(ref_id,name,generator,params,refdes) VALUES (%s,'G','ewod_pad_array','{}','R1')",
            (ref.id,),
        )
        if collateral == "router":
            board = conn.execute(
                "SELECT board_id FROM pcb_boards WHERE ref_id=%s", (ref.id,)
            ).fetchone()[0]
            net = conn.execute(
                "SELECT net_id FROM pcb_nets WHERE ref_id=%s", (ref.id,)
            ).fetchone()[0]
            route = conn.execute(
                "INSERT INTO pcb_routes(board_id,net_id,status) VALUES (%s,%s,'realized') RETURNING route_id",
                (board, net),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO pcb_copper(board_id,net_id,route_id,ctype,layer,geom) VALUES (%s,%s,%s,'track','F.Cu',%s)",
                (
                    board,
                    net,
                    route,
                    Jsonb({"points": [[10, 10], [15, 10]], "width_mm": 0.2}),
                ),
            )
    with store.pcb_view_tx() as conn:
        before = pcb_design.capture_view(store, ref, conn=conn)
    context = {
        **before.context,
        "selected": [next(i for i in before.instances if i["refdes"] == "R1")],
    }
    error = pcb_design.dry_run(
        hub, ref, context, [{"op": "move", "refdes": "R1", "x": 11.0}]
    )
    assert "collateral/group/router" in (error or ""), error
    with store.pcb_view_tx() as conn:
        after = pcb_design.capture_view(store, ref, conn=conn)
    assert pcb_design._stable(after.raw) == pcb_design._stable(before.raw)
    assert history.list_revisions(store, ref.id) == []
    assert history.list_checkpoints(store, ref.id) == []
