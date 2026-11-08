"""A queued job says where it waits and why (gr495292).

``op='route'`` jobs sat ``STATUS:queued`` for minutes while only other jobs
were running, and ``get(kind='job')`` showed neither a position nor a
reason. The view now renders ``queue:`` lines for ``STATUS:queued`` rows:
position in the executor's claim order, what runs ahead, and the claim
gates visible from the row (suspension, node pin).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from precis.dispatch import Hub
from precis.handlers.job import JobHandler
from precis.store import Store
from precis.store.types import Tag


def _job(
    store: Store,
    status: str,
    *,
    job_type: str = "pcb_place",
    executor: str = "job_inproc",
    prio: int | None = None,
    params: dict[str, str] | None = None,
    origin: str | None = None,
) -> int:
    meta: dict[str, object] = {"job_type": job_type, "executor": executor}
    if params:
        meta["params"] = params
    if origin is not None:
        meta["origin"] = origin
    ref = store.insert_ref(kind="job", slug=None, title=job_type, meta=meta)
    if prio is not None:
        with store.pool.connection() as conn:
            conn.execute("UPDATE refs SET prio = %s WHERE ref_id = %s", (prio, ref.id))
    store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="agent")
    return ref.id


def _body(store: Store, job_id: int) -> str:
    return JobHandler(hub=Hub(store=store, embedder=None)).get(id=job_id).body


def test_queued_job_shows_position_and_what_runs_ahead(store: Store) -> None:
    _job(store, "running", job_type="materialize")
    first = _job(store, "queued")
    second = _job(store, "queued")
    _job(store, "queued", executor="ssh_node")
    body = _body(store, second)
    assert (
        "queue: position 2 of 2 queued for executor 'job_inproc' — class "
        "background (1 ahead" in body
    )
    assert "claim order is class, then priority, then time queued" in body
    assert "queue: running now on 'job_inproc': 1 (materialize x1)" in body
    assert "queue: position 1 of 2" in _body(store, first)


def test_queue_shows_origin_class_and_per_class_ahead(store: Store) -> None:
    # A background band already queued; a fresh agent (interactive) job
    # behind it outranks it, so the agent row is position 1 and the
    # background rows report behind it (slice 6, D6).
    _job(store, "queued", origin="background")
    _job(store, "queued", origin="background")
    agent = _job(store, "queued", origin="agent")
    abody = _body(store, agent)
    assert "— class agent (0 ahead" in abody
    # The last-ranked background row is behind the agent row + its sibling.
    tail = _job(store, "queued", origin="background")
    tbody = _body(store, tail)
    assert "— class background (3 ahead" in tbody
    assert "queue: ahead by class: agent x1, background x2" in tbody


def test_higher_priority_job_jumps_the_position(store: Store) -> None:
    _job(store, "queued")
    urgent = _job(store, "queued", prio=1)
    assert "queue: position 1 of 2" in _body(store, urgent)


def test_idle_executor_is_called_out(store: Store) -> None:
    job = _job(store, "queued")
    assert "queue: nothing running on 'job_inproc'" in _body(store, job)


def test_node_pin_is_a_reason(store: Store) -> None:
    job = _job(store, "queued", params={"target_node": "box-a"})
    assert "queue: pinned to node 'box-a'" in _body(store, job)


def test_suspended_type_is_held_not_ranked(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_SUSPENDED_JOB_TYPES", "pcb_place")
    body = _body(store, _job(store, "queued"))
    assert "queue: held — job_type 'pcb_place'" in body
    assert "queue: position" not in body


def test_terminal_job_has_no_queue_lines(store: Store) -> None:
    assert "queue:" not in _body(store, _job(store, "succeeded"))


# ── Stale-read guard (gr592853) ─────────────────────────────────────
#
# The card's ref/tags reads and the queue scan run on separate
# checkouts; a stale one rendered a finished draft_export as "queued ·
# nothing running" for minutes after the terminal flip committed. The
# terminal STATUS: flip lands in the same transaction as the
# meta.finished_at stamp, so the card reconciles a live status against
# one fresh probe — and never renders a terminal row as queued.


def _render_with_tags(store: Store, job_id: int, tags: list[Tag]) -> str:
    """The job card as rendered from a caller-supplied (stale) tag list."""
    handler = JobHandler(hub=Hub(store=store, embedder=None))
    ref = handler._resolve_live_ref(job_id)
    return handler._render_one(ref, tags)


def test_card_shows_updated_at_and_finished_at(store: Store) -> None:
    body = _body(store, _job(store, "succeeded"))
    assert "updated_at:" in body
    assert "finished_at:" in body


def test_finished_stamp_contradiction_never_renders_queued(
    store: Store,
) -> None:
    """The prod shape: tags still read ``queued`` while the row already
    carries the terminal stamp — the card must not claim 'queued'."""
    job = _job(store, "queued")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
            (json.dumps({"finished_at": datetime.now(UTC).isoformat()}), job),
        )
        conn.commit()
    body = _body(store, job)
    assert "\nstatus: queued\n" not in f"\n{body}\n"
    assert "queue:" not in body
    assert "stale-read" in body
    assert "finished_at:" in body


def test_stale_tag_read_is_corrected_to_terminal(store: Store) -> None:
    """A stale tag list handed to the card is corrected by the fresh
    probe: the finished row renders 'succeeded', never 'queued'."""
    job = _job(store, "succeeded")
    body = _render_with_tags(store, job, [Tag.closed("STATUS", "queued")])
    assert "\nstatus: succeeded" in f"\n{body}"
    assert "stale-read" in body
    assert "queue:" not in body


def test_requeued_after_terminal_renders_queued_normally(store: Store) -> None:
    """A live re-tag after a terminal run keeps the last run's
    ``finished_at`` — that job really is queued again; the stamp alone
    must not flip the card to terminal."""
    job = _job(store, "succeeded")
    store.add_tag(
        job,
        Tag.closed("STATUS", "queued"),
        set_by="agent",
        replace_prefix=True,
    )
    body = _body(store, job)
    assert "\nstatus: queued" in f"\n{body}"
    assert "stale-read" not in body
    assert "queue:" in body


def test_claimed_between_reads_reports_fresh_status(store: Store) -> None:
    """Tags say queued (stale) while a fresh probe sees the row running —
    the card corrects to the fresh status instead of inventing a queue
    position."""
    job = _job(store, "running")
    body = _render_with_tags(store, job, [Tag.closed("STATUS", "queued")])
    assert "\nstatus: running" in f"\n{body}"
    assert "stale-read" in body
    assert "queue: position" not in body


def test_queue_lines_report_fresh_status_over_stale_card(store: Store) -> None:
    """The queue scan's own STATUS probe — same snapshot as the position
    math — names the fresh truth when the card's read was stale."""
    handler = JobHandler(hub=Hub(store=store, embedder=None))
    job = _job(store, "running")
    ref = handler._resolve_live_ref(job)
    lines = handler._queue_lines(ref)
    assert any("STATUS:running" in line for line in lines)
    assert not any("position" in line for line in lines)
    assert any("running now on 'job_inproc': 1" in line for line in lines)


def test_queue_lines_suppress_advice_on_terminal_row(store: Store) -> None:
    """A terminal row must never get 'position N' / 'nothing running …
    if this stays queued' — the probe suppresses both."""
    handler = JobHandler(hub=Hub(store=store, embedder=None))
    job = _job(store, "succeeded")
    ref = handler._resolve_live_ref(job)
    lines = handler._queue_lines(ref)
    assert any("STATUS:succeeded" in line for line in lines)
    assert not any("position" in line for line in lines)
    assert not any("if this stays queued" in line for line in lines)


# ── Release band in the readout (gr584136, review td584420) ────────────
#
# The release legs rank at fractional class 2.5 (``job_type``-keyed); the
# readout used to parse the per-class counts with ``int(k)`` — a ValueError
# on ``"2.5"`` — and mislabel the band ``recurring``.


def _age_queued_tag(store: Store, ref_id: int, secs: float) -> None:
    """Back-date a job's ``STATUS:queued`` tag by ``secs`` (ageing input)."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_tags rt SET created_at = now() - "
            "make_interval(secs => %s) "
            "FROM tags t WHERE t.tag_id = rt.tag_id AND rt.ref_id = %s "
            "AND t.namespace = 'STATUS' AND t.value = 'queued'",
            (secs, ref_id),
        )
        conn.commit()


def test_release_leg_reports_its_own_band(store: Store) -> None:
    leg = _job(store, "queued", job_type="ci_verdict_poll")
    body = _body(store, leg)
    assert "— class release (0 ahead" in body
    assert "recurring" not in body


def test_queued_behind_release_leg_parses_fractional_class(store: Store) -> None:
    _job(store, "queued", job_type="ci_verdict_poll")
    follower = _job(store, "queued")
    body = _body(store, follower)
    assert "queue: position 2 of 2" in body
    assert "queue: ahead by class: release x1" in body


def test_release_leg_freezes_ageing_in_readout(store: Store) -> None:
    """The readout mirrors the claim's release freeze: while a leg is
    claimable a background row's class-ageing promotion is floored at its
    own class, so a 2-hour-old row stays rank 3 behind the 2.5 leg.
    Without ``release_freeze`` it would promote to 2.5, tie the leg, and
    win on fully-aged prio — reporting position 1 where the claim puts
    it second."""
    bg = _job(store, "queued", origin="background")
    _age_queued_tag(store, bg, 7200)
    leg = _job(store, "queued", job_type="ci_verdict_poll")
    assert "queue: position 1 of 2" in _body(store, leg)
    body = _body(store, bg)
    assert "queue: position 2 of 2" in body
    assert "aged up to" not in body
