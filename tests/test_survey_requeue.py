"""``requeue_after_body_replace`` + the pending-flag drain (gr632959).

A body swap re-queues ``survey_extract`` only for a paper that already
had one; a live pass gets a durable ``meta.survey_revision_request``
(the next slice restarts over the new body) instead of a second mint;
and the prior-job lookup resolves paper identity the way the job itself
does — ``pa<id>`` handles and refs merged into the paper included, not
just a bare ref_id.
"""

from __future__ import annotations

from typing import Any

import psycopg.errors
import pytest
from psycopg.types.json import Jsonb

import precis.workers.survey_requeue as survey_requeue
from precis.store import Store
from precis.store.types import Tag
from precis.workers._mint import mint_background_job
from precis.workers.executors._yield import Done, WakeWhen, Yield
from precis.workers.executors.coordinator import _persist_dispatch_result
from precis.workers.survey_requeue import (
    drain_pending_requeues,
    requeue_after_body_replace,
)


def _paper(store: Store, slug: str) -> int:
    return store.insert_ref(
        kind="paper", slug=slug, title=f"T {slug}", provider="manual", meta={}
    ).id


def _body(store: Store, rid: int, n: int = 3) -> None:
    with store.pool.connection() as conn:
        for i in range(n):
            conn.execute(
                "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text) "
                "VALUES (%s, 'system', %s, 'paragraph', %s)",
                (rid, i, f"body chunk {i}"),
            )
        conn.commit()


def _mint_survey(
    store: Store, paper: Any, *, status: str = "succeeded", **params: Any
) -> int:
    """A ``survey_extract`` job for ``paper`` in the given STATUS."""
    with store.tx() as conn:
        ref = mint_background_job(
            store,
            conn=conn,
            job_type="survey_extract",
            executor="coordinator",
            title="old",
            params={"paper": paper, **params},
            status="queued",
        )
    if status != "queued":
        store.add_tag(
            ref.id,
            Tag.parse_strict(f"STATUS:{status}"),
            set_by="system",
            replace_prefix=True,
        )
    return int(ref.id)


def _jobs(store: Store, ref_id: int, *also: int) -> list[dict[str, Any]]:
    """Every survey_extract job naming ``ref_id`` (or ``also`` ids),
    oldest first — both the bare and ``pa<id>`` param spellings."""
    forms = [f for i in (ref_id, *also) for f in (str(i), f"pa{i}")]
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT meta FROM refs WHERE kind = 'job' "
            "AND meta ->> 'job_type' = 'survey_extract' "
            "AND meta -> 'params' ->> 'paper' = ANY(%s) ORDER BY ref_id",
            (forms,),
        ).fetchall()
    return [r[0] for r in rows]


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return dict(row[0])


def _flag(store: Store, rid: int) -> None:
    """The stamp ``db_writer``'s markup_refetch REPLACE writes."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || "
            "jsonb_build_object('survey_requeue_pending', now()) "
            "WHERE ref_id = %s",
            (rid,),
        )
        conn.commit()


def test_never_extracted_paper_is_left_alone(store: Store) -> None:
    rid = _paper(store, "nx1")
    with store.tx() as conn:
        assert requeue_after_body_replace(store, conn, rid, new_chunks=5) is None
    assert _jobs(store, rid) == []


def test_extracted_paper_requeues_once_with_prior_params(store: Store) -> None:
    rid = _paper(store, "ex1")
    _mint_survey(store, rid, run_token="old-run", tier="big")
    with store.tx() as conn:
        first = requeue_after_body_replace(store, conn, rid, new_chunks=161)
    with store.tx() as conn:
        second = requeue_after_body_replace(store, conn, rid, new_chunks=161)
    assert first is not None and first == second
    jobs = _jobs(store, rid)
    assert len(jobs) == 2
    params = jobs[1]["params"]
    assert isinstance(params, dict)
    assert params["tier"] == "big" and "run_token" not in params


def test_pa_handle_prior_is_found(store: Store) -> None:
    """A prior job minted with ``paper='pa<ref_id>'`` counts the same."""
    rid = _paper(store, "ex2")
    _mint_survey(store, f"pa{rid}")
    with store.tx() as conn:
        job_id = requeue_after_body_replace(store, conn, rid, new_chunks=10)
    assert job_id is not None
    assert len(_jobs(store, rid)) == 2


def test_merged_ancestor_handle_resolves_to_survivor(store: Store) -> None:
    """A job minted ``paper='pa<loser>'`` before the merge names the
    survivor — ``resolve_handle`` follows ``meta.superseded_by``, so the
    prior-job lookup walks the same chain."""
    loser = _paper(store, "los1")
    rid = _paper(store, "ex3")
    _mint_survey(store, f"pa{loser}")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || %s, retired_at = now() WHERE ref_id = %s",
            (Jsonb({"superseded_by": str(rid)}), loser),
        )
        conn.commit()
    with store.tx() as conn:
        job_id = requeue_after_body_replace(store, conn, rid, new_chunks=10)
    assert job_id is not None
    assert len(_jobs(store, rid, loser)) == 2


def test_live_job_revision_requested_not_double_minted(store: Store) -> None:
    """A live pass holding a cursor over the outgoing body gets a durable
    revision request, not a checkpoint clear (an in-flight slice would
    silently restore a cleared checkpoint) and not a twin mint: its
    next claim restarts at chunk 0 of the new body (gr632959 review)."""
    rid = _paper(store, "lv1")
    job_id = _mint_survey(store, rid, status="queued")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || %s WHERE ref_id = %s",
            (Jsonb({"coordinator_state": {"phase": "extract", "idx": 40}}), job_id),
        )
        conn.commit()
    with store.tx() as conn:
        out = requeue_after_body_replace(store, conn, rid, new_chunks=100)
    assert out == job_id
    assert len(_jobs(store, rid)) == 1
    meta = _meta(store, job_id)
    assert meta["coordinator_state"] == {"phase": "extract", "idx": 40}
    assert meta["survey_revision_request"]["paper"] == rid
    assert meta["survey_revision_request"]["chunks"] == 100


def test_revision_request_survives_in_flight_yield_persist(store: Store) -> None:
    """Race A (gr632959 review): the requeue stamps a revision request on
    a live job; a concurrent slice's ``Yield`` persist overwrites
    ``coordinator_state`` but the top-level meta merge must leave the
    request standing, so the next claim still restarts over the new body."""
    rid = _paper(store, "ra1")
    job_id = _mint_survey(store, rid, status="yielded")
    with store.tx() as conn:
        assert requeue_after_body_replace(store, conn, rid, new_chunks=100) == job_id
    _persist_dispatch_result(
        store,
        job_id,
        Yield(
            state={"phase": "extract", "idx": 40, "body_mark": "9:10"},
            wake_when=WakeWhen("at_time", {"ts": 1.0}),
        ),
    )
    meta = _meta(store, job_id)
    assert meta["coordinator_state"]["idx"] == 40  # the in-flight persist landed
    assert meta["survey_revision_request"]["paper"] == rid  # …request survived


def test_done_race_sweeps_terminal_job_into_followup_mint(store: Store) -> None:
    """Race B (gr632959 review): the in-flight slice persisted ``Done``
    for the outgoing body after the request was stamped — the job went
    terminal with the request on its row; the drain sweep mints the
    follow-up pass and clears the request."""
    rid = _paper(store, "rb1")
    job_id = _mint_survey(store, rid, status="running", run_token="old-run")
    with store.tx() as conn:
        requeue_after_body_replace(store, conn, rid, new_chunks=100)
    _persist_dispatch_result(
        store, job_id, Done(summary="survey_extract done (old body)")
    )
    assert drain_pending_requeues(store) == 1
    jobs = _jobs(store, rid)
    assert len(jobs) == 2
    assert "run_token" not in jobs[1]["params"]
    assert "survey_revision_request" not in _meta(store, job_id)
    assert drain_pending_requeues(store) == 0


def test_sweep_leaves_live_jobs_request_for_dispatch(store: Store) -> None:
    """A live job consumes its own request at the next claim — the sweep
    must not mint for it or clear the key."""
    rid = _paper(store, "rv3")
    job_id = _mint_survey(store, rid, status="yielded")
    with store.tx() as conn:
        requeue_after_body_replace(store, conn, rid, new_chunks=50)
    assert drain_pending_requeues(store) == 0
    assert "survey_revision_request" in _meta(store, job_id)
    assert len(_jobs(store, rid)) == 1


def _stamp_restart(store: Store, job_id: int, paper: int) -> None:
    """The consume's durable marker: the request was atomically swapped
    for a ``coordinator_state.restart`` checkpoint, then the slice never
    returned (a coordinator exception, or a crash the stuck-job sweeper
    later fails)."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
            (
                Jsonb(
                    {
                        "coordinator_state": {
                            "phase": "extract",
                            "restart": {"paper": paper, "chunks": 100},
                        }
                    }
                ),
                job_id,
            ),
        )
        conn.commit()


def test_terminal_restart_checkpoint_sweeps_into_followup(store: Store) -> None:
    """gr632959 review r2: the consume deletes the request before the
    restarted slice runs — a coordinator exception terminally fails the
    job and a crashed ``running`` job is never resumed, so both leave a
    terminal job carrying only the ``restart`` marker. The sweep mints
    the lost pass from it and clears the checkpoint."""
    rid = _paper(store, "rc1")
    job_id = _mint_survey(store, rid, status="failed", run_token="old")
    _stamp_restart(store, job_id, rid)
    assert drain_pending_requeues(store) == 1
    jobs = _jobs(store, rid)
    assert len(jobs) == 2
    assert "run_token" not in jobs[1]["params"]
    assert "restart" not in _meta(store, job_id).get("coordinator_state", {})
    assert drain_pending_requeues(store) == 0


def test_live_job_restart_checkpoint_left_for_dispatch(store: Store) -> None:
    """A live job still holding the restart marker is inside the consume
    window — its next claim restarts over the new body itself. The sweep
    must not mint or clear."""
    rid = _paper(store, "rc2")
    job_id = _mint_survey(store, rid, status="running")
    _stamp_restart(store, job_id, rid)
    assert drain_pending_requeues(store) == 0
    assert "restart" in _meta(store, job_id)["coordinator_state"]
    assert len(_jobs(store, rid)) == 1


def test_terminal_restart_cleared_when_live_pass_covers(store: Store) -> None:
    """A terminal job's restart marker is stale once another pass is
    live for the paper — the sweep clears it without minting a twin."""
    rid = _paper(store, "rc3")
    dead = _mint_survey(store, rid, status="failed")
    _mint_survey(store, rid, status="queued")
    _stamp_restart(store, dead, rid)
    assert drain_pending_requeues(store) == 1
    assert len(_jobs(store, rid)) == 2
    assert "restart" not in _meta(store, dead).get("coordinator_state", {})


def _park_at_await(store: Store, job_id: int) -> None:
    """A live job parked at ``await_accept`` over the outgoing body:
    its extract pass finished and it waits on the accept child — the
    phase never consults ``body_mark``, so without a stamped request it
    can never re-read the swapped chunks."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
            (
                Jsonb(
                    {
                        "coordinator_state": {
                            "phase": "await_accept",
                            "accept_child": 424242,
                            "run_token": "survey-extract:old-body",
                        }
                    }
                ),
                job_id,
            ),
        )
        conn.commit()


def test_terminal_intent_transfers_to_live_awaiting_pass(store: Store) -> None:
    """gr632959 review r3: a terminal job's revision intent must not be
    cleared just because a sibling extractor is live — parked at
    ``await_accept`` over the outgoing body, that sibling finishes its
    stale pass without ever re-reading the swapped chunks. The sweep
    transfers the request to it instead of minting a twin."""
    rid = _paper(store, "rc4")
    dead = _mint_survey(store, rid, status="failed")
    live = _mint_survey(store, rid, status="yielded")
    _park_at_await(store, live)
    _stamp_restart(store, dead, rid)
    assert drain_pending_requeues(store) == 1
    assert len(_jobs(store, rid)) == 2  # the transfer — no third mint
    live_meta = _meta(store, live)
    assert live_meta["survey_revision_request"]["paper"] == rid
    assert live_meta["survey_revision_request"]["transferred_from"] == dead
    assert "restart" not in _meta(store, dead).get("coordinator_state", {})
    assert drain_pending_requeues(store) == 0


def test_terminal_intent_not_transferred_to_covered_pass(store: Store) -> None:
    """The transfer's twin check: a live pass whose extract cursor
    carries ``body_mark`` re-derives the mark at resume and restarts on
    drift by itself — it already covers the swap, so the terminal job's
    intent clears without a redundant stamp."""
    rid = _paper(store, "rc5")
    dead = _mint_survey(store, rid, status="failed")
    live = _mint_survey(store, rid, status="yielded")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
            (
                Jsonb(
                    {
                        "coordinator_state": {
                            "phase": "extract",
                            "idx": 4,
                            "body_mark": "9:9",
                        }
                    }
                ),
                live,
            ),
        )
        conn.commit()
    _stamp_restart(store, dead, rid)
    assert drain_pending_requeues(store) == 1
    assert "survey_revision_request" not in _meta(store, live)
    assert "restart" not in _meta(store, dead).get("coordinator_state", {})


def _running_extract_cursor(store: Store, job_id: int) -> None:
    """A live job whose last checkpoint is a ``body_mark`` extract
    cursor AND whose STATUS is ``running``: its in-flight slice already
    passed the mark check with the outgoing chunks loaded and can still
    finish into ``await_accept`` or ``Done`` without re-checking."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
            (
                Jsonb(
                    {
                        "coordinator_state": {
                            "phase": "extract",
                            "idx": 4,
                            "body_mark": "9:9",
                        }
                    }
                ),
                job_id,
            ),
        )
        conn.commit()


def test_terminal_intent_transfers_to_running_extract_pass(store: Store) -> None:
    """gr632959 review r4: ``body_mark`` is coverage only while the pass
    is parked — a ``running`` sibling's in-flight slice already passed
    the mark check over the outgoing chunks and can finish into
    ``await_accept`` or ``Done`` without re-checking. The sweep stamps
    the durable request on it so the rerun survives whatever the slice
    persists, instead of trusting the checkpoint."""
    rid = _paper(store, "rc7")
    dead = _mint_survey(store, rid, status="failed")
    live = _mint_survey(store, rid, status="running")
    _running_extract_cursor(store, live)
    _stamp_restart(store, dead, rid)
    assert drain_pending_requeues(store) == 1
    assert len(_jobs(store, rid)) == 2  # the transfer — no third mint
    live_meta = _meta(store, live)
    assert live_meta["survey_revision_request"]["paper"] == rid
    assert live_meta["survey_revision_request"]["transferred_from"] == dead
    assert "restart" not in _meta(store, dead).get("coordinator_state", {})
    assert drain_pending_requeues(store) == 0


def test_terminal_intent_transfers_to_running_first_slice(store: Store) -> None:
    """The same race with nothing checkpointed yet: a ``running`` first
    slice holds no ``coordinator_state`` — its loaded chunks may be the
    outgoing body and it can finish without ever yielding a cursor for
    the mark check to catch."""
    rid = _paper(store, "rc8")
    dead = _mint_survey(store, rid, status="failed")
    live = _mint_survey(store, rid, status="running")
    _stamp_restart(store, dead, rid)
    assert drain_pending_requeues(store) == 1
    assert len(_jobs(store, rid)) == 2
    live_meta = _meta(store, live)
    assert live_meta["survey_revision_request"]["paper"] == rid
    assert live_meta["survey_revision_request"]["transferred_from"] == dead


def test_terminal_intent_transfers_to_cancel_requested_pass(store: Store) -> None:
    """``cancel_requested`` is still an executing slice — the cancel
    lands at the next boundary, which may be the pass finishing into
    ``await_accept`` over the outgoing body."""
    rid = _paper(store, "rc9")
    dead = _mint_survey(store, rid, status="failed")
    live = _mint_survey(store, rid, status="cancel_requested")
    _running_extract_cursor(store, live)
    _stamp_restart(store, dead, rid)
    assert drain_pending_requeues(store) == 1
    assert len(_jobs(store, rid)) == 2
    live_meta = _meta(store, live)
    assert live_meta["survey_revision_request"]["paper"] == rid
    assert live_meta["survey_revision_request"]["transferred_from"] == dead


def test_terminal_request_also_transfers_not_only_restart(store: Store) -> None:
    """The same transfer when the terminal marker is the unconsumed
    ``meta.survey_revision_request`` itself (the ``Done`` race), not
    the consumed ``restart`` checkpoint."""
    rid = _paper(store, "rc6")
    dead = _mint_survey(store, rid, status="succeeded")
    live = _mint_survey(store, rid, status="yielded")
    _park_at_await(store, live)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
            (
                Jsonb({"survey_revision_request": {"paper": rid, "chunks": 100}}),
                dead,
            ),
        )
        conn.commit()
    assert drain_pending_requeues(store) == 1
    assert len(_jobs(store, rid)) == 2
    assert _meta(store, live)["survey_revision_request"]["paper"] == rid
    assert "survey_revision_request" not in _meta(store, dead)


def test_sweep_coverage_reads_under_sibling_row_lock(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr632959 td663790 review: the coverage pair — sibling ``meta``
    and sibling STATUS — must serialize with the sibling's own
    transitions. A ``_persist_dispatch_result`` parking the sibling at
    ``await_accept`` between the two reads leaves the stale extract
    checkpoint (``body_mark`` present) paired with the fresh parked
    status: a covered verdict, the terminal job's intent cleared with
    no transfer, the rerun lost while the sibling finishes its stale
    pass. The sweep reads the pair under the sibling's ``refs`` row
    lock, so the parking write has to wait out the transaction —
    probed here with a ``lock_timeout`` write from a second connection
    fired at exactly the seam between the two reads."""
    rid = _paper(store, "rc10")
    dead = _mint_survey(store, rid, status="failed")
    live = _mint_survey(store, rid, status="running")
    _running_extract_cursor(store, live)
    _stamp_restart(store, dead, rid)

    probe: list[str] = []
    real_status = survey_requeue._job_status

    def _racing_status(conn: Any, ref_id: int) -> str:
        if ref_id != live or probe:
            return real_status(conn, ref_id)
        # The interleave the review named: as if
        # ``_persist_dispatch_result`` commits the ``await_accept``
        # park — checkpoint plus waiting status — right here, between
        # the sweep's meta read and its STATUS read.
        with store.pool.connection() as other:
            other.execute("SET LOCAL lock_timeout = '200ms'")
            try:
                other.execute(
                    "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
                    (
                        Jsonb({"coordinator_state": {"phase": "await_accept"}}),
                        live,
                    ),
                )
            except psycopg.errors.LockNotAvailable:
                probe.append("blocked")
            else:
                store.add_tag(
                    live,
                    Tag.parse_strict("STATUS:waiting_children"),
                    set_by="system",
                    replace_prefix=True,
                    conn=other,
                )
                other.commit()
                probe.append("committed")
        return real_status(conn, ref_id)

    monkeypatch.setattr(survey_requeue, "_job_status", _racing_status)
    assert drain_pending_requeues(store) == 1
    # The sibling lock held across the whole inspection: the park could
    # not commit inside the window, so the sweep decided on the
    # pre-transition pair — ``running`` is uncovered — and transferred.
    assert probe == ["blocked"]
    live_meta = _meta(store, live)
    assert live_meta["survey_revision_request"]["paper"] == rid
    assert live_meta["survey_revision_request"]["transferred_from"] == dead
    assert live_meta["coordinator_state"]["phase"] == "extract"
    assert "restart" not in _meta(store, dead).get("coordinator_state", {})
    assert drain_pending_requeues(store) == 0


def test_empty_new_body_does_not_requeue(store: Store) -> None:
    rid = _paper(store, "em1")
    _mint_survey(store, rid)
    with store.tx() as conn:
        assert requeue_after_body_replace(store, conn, rid, new_chunks=0) is None
    assert len(_jobs(store, rid)) == 1


def test_drain_mints_for_extracted_paper_and_clears_flag(store: Store) -> None:
    """The ingest-side path's flag: the drain mints the requeue and
    consumes the flag in the same transaction."""
    rid = _paper(store, "dr1")
    _body(store, rid)
    _mint_survey(store, rid)
    _flag(store, rid)
    assert drain_pending_requeues(store) == 1
    assert len(_jobs(store, rid)) == 2
    assert "survey_requeue_pending" not in _meta(store, rid)
    assert drain_pending_requeues(store) == 0


def test_drain_never_extracted_clears_flag_without_mint(store: Store) -> None:
    rid = _paper(store, "dr2")
    _body(store, rid)
    _flag(store, rid)
    assert drain_pending_requeues(store) == 1
    assert _jobs(store, rid) == []
    assert "survey_requeue_pending" not in _meta(store, rid)


def test_drain_live_job_stamps_request_and_clears_flag(store: Store) -> None:
    """A flagged paper whose extraction is still parked keeps its job —
    the drain stamps the revision request rather than minting a twin."""
    rid = _paper(store, "dr3")
    _body(store, rid)
    job_id = _mint_survey(store, rid, status="queued")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || %s WHERE ref_id = %s",
            (Jsonb({"coordinator_state": {"phase": "extract", "idx": 9}}), job_id),
        )
        conn.commit()
    _flag(store, rid)
    assert drain_pending_requeues(store) == 1
    assert len(_jobs(store, rid)) == 1
    job_meta = _meta(store, job_id)
    assert job_meta["coordinator_state"] == {"phase": "extract", "idx": 9}
    assert job_meta["survey_revision_request"]["paper"] == rid
    assert "survey_requeue_pending" not in _meta(store, rid)
