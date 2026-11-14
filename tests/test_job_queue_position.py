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
from precis.store.types import Ref, Tag


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
    assert (
        "claim order is class, then factory-pipeline ops, then priority, "
        "then time queued" in body
    )
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
    """The queue scan's own STATUS column — same statement as the
    position math — names the fresh truth when the card's read was
    stale."""
    handler = JobHandler(hub=Hub(store=store, embedder=None))
    job = _job(store, "running")
    ref = handler._resolve_live_ref(job)
    lines, _, _ = handler._queue_lines(ref)
    assert any("STATUS:running" in line for line in lines)
    assert not any("position" in line for line in lines)
    assert any("running now on 'job_inproc': 1" in line for line in lines)


def test_queue_lines_suppress_advice_on_terminal_row(store: Store) -> None:
    """A terminal row must never get 'position N' / 'nothing running …
    if this stays queued' — the scan's own status column suppresses
    both."""
    handler = JobHandler(hub=Hub(store=store, embedder=None))
    job = _job(store, "succeeded")
    ref = handler._resolve_live_ref(job)
    lines, scan_ref, scan_status = handler._queue_lines(ref)
    assert any("STATUS:succeeded" in line for line in lines)
    assert not any("position" in line for line in lines)
    assert not any("if this stays queued" in line for line in lines)
    assert scan_ref is not None and scan_status == "succeeded"


def test_completion_between_reconcile_and_scan_renders_terminal(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The td595284 residual window: the reconcile read says ``queued``,
    the terminal flip lands before the queue scan — the card must render
    the scan's snapshot (``status: succeeded``), never a ``queued``
    label over a ``STATUS:succeeded`` queue line."""
    handler = JobHandler(hub=Hub(store=store, embedder=None))
    job = _job(store, "queued")
    ref = handler._resolve_live_ref(job)
    tags = store.tags_for(job)
    real = handler._reconcile_status_read

    def _complete_after_reconcile(
        r: Ref, status: str | None
    ) -> tuple[Ref, str | None, str | None]:
        out = real(r, status)
        # The terminal flip lands here — between the status read and
        # the queue scan — stamping finished_at like the real flip.
        store.add_tag(
            job,
            Tag.closed("STATUS", "succeeded"),
            set_by="agent",
            replace_prefix=True,
        )
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
                (json.dumps({"finished_at": datetime.now(UTC).isoformat()}), job),
            )
            conn.commit()
        return out

    monkeypatch.setattr(handler, "_reconcile_status_read", _complete_after_reconcile)
    body = handler._render_one(ref, tags)
    assert "\nstatus: succeeded" in f"\n{body}"
    assert "\nstatus: queued\n" not in f"\n{body}\n"
    assert "finished_at:" in body
    assert "stale-read" in body
    assert "STATUS:succeeded" in body
    assert "queue: position" not in body


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


# ── Factory-pipeline term in the readout (gr657333, review td657493) ──
#
# The claim ranks a ``run_slice``/``land_slice`` ahead of same-class bulk
# rows; the position math must count the same way or a slice still
# reports older seeds ahead despite claiming before them.


def test_factory_op_reports_ahead_of_same_class_bulk(store: Store) -> None:
    """The gripe's shape on the card: a fresh ``run_slice`` reports
    position 1 over the older bulk backlog it claims before — not
    ``position N`` behind it."""
    for _ in range(3):
        _job(store, "queued", origin="background", job_type="autocatpath_seed")
    run = _job(store, "queued", origin="background", job_type="run_slice")
    body = _body(store, run)
    assert "queue: position 1 of 4" in body
    assert "factory-pipeline ops" in body


def test_bulk_row_counts_the_factory_op_ahead(store: Store) -> None:
    """The inverse read: a bulk row behind a factory op sees it named —
    ``ahead by class`` alone would show only 'background x1' and hide
    why the younger job outranks it."""
    _job(store, "queued", origin="background", job_type="run_slice")
    seed = _job(store, "queued", origin="background", job_type="autocatpath_seed")
    body = _body(store, seed)
    assert "queue: position 2 of 2" in body
    assert "queue: ahead by class: background x1 (1 factory-pipeline op ahead)" in body


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


# ── dark-lane callout + outage attribution (gr592838 want (b)) ──────────
#
# An executor whose claimable queue ages past the dark floor with no
# claimer and nothing running is named as OUT on the card — with the open
# alert and gripe that own the outage, so the reader isn't left matching
# "nothing running" to the cause themselves.


def test_dark_lane_is_called_out_and_attributed(store: Store) -> None:
    from precis.alerts import raise_alert

    job = _job(store, "queued")
    _age_queued_tag(store, job, 1200)  # past the 15m dark floor
    aid, _ = raise_alert(
        store,
        source="jobstarve",
        fingerprint="control:job_inproc",
        title="[jobstarve] job_inproc control starved",
        detail="seeded",
        severity="info",
    )
    from precis.handlers.gripe import GripeHandler

    gout = GripeHandler(hub=Hub(store=store, embedder=None)).put(
        text="job_inproc lane not claiming"
    )
    gid = int(gout.body.split("id=")[1].split()[0].rstrip(",.()"))
    body = _body(store, job)
    assert "queue: lane alert: 'job_inproc' reads dark" in body
    assert f"al{aid} jobstarve/control:job_inproc" in body
    assert f"gr{gid} job_inproc lane not claiming" in body


def test_dark_lane_unfiled_outage_says_so(store: Store) -> None:
    job = _job(store, "queued")
    _age_queued_tag(store, job, 1200)
    body = _body(store, job)
    assert "queue: lane alert: 'job_inproc' reads dark" in body
    assert "no open alert or gripe names this lane" in body


def test_fresh_queue_is_not_dark(store: Store) -> None:
    assert "lane alert" not in _body(store, _job(store, "queued"))


def test_running_lane_is_not_dark(store: Store) -> None:
    """A lane with work running has a claimer — queued age alone never
    flags it."""
    _job(store, "running", job_type="materialize")
    job = _job(store, "queued")
    _age_queued_tag(store, job, 1200)
    assert "lane alert" not in _body(store, job)
    assert "queue: running now on 'job_inproc': 1" in _body(store, job)


def test_sibling_without_started_at_keeps_dark(store: Store) -> None:
    """A finished sibling that never stamped ``meta.started_at`` is not
    evidence the lane was claimed recently — the flag still fires."""
    _job(store, "succeeded", job_type="materialize")
    job = _job(store, "queued")
    _age_queued_tag(store, job, 1200)
    assert "lane alert" in _body(store, job)


def test_started_at_inside_window_clears_dark(store: Store) -> None:
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="t",
        meta={
            "job_type": "materialize",
            "executor": "job_inproc",
            "started_at": datetime.now(UTC).isoformat(),
        },
    )
    store.add_tag(ref.id, Tag.closed("STATUS", "succeeded"), set_by="agent")
    job = _job(store, "queued")
    _age_queued_tag(store, job, 1200)
    assert "lane alert" not in _body(store, job)


# ── dark read honours the parent gate (review td677026) ─────────────
#
# ``_lane_dark`` must count what the lane's claim would take — the shared
# ``parent_gate_exclusion_sql`` predicate — not every ``STATUS:queued``
# row: a job behind a paused (non-terminal) parent on a parent-gated
# executor waits on its parent, never on a claimer, so a lane holding
# only such rows is a healthy idle lane, not a dark one.


def _paused_parent_todo(store: Store, *, terminal: bool = False) -> int:
    """A live todo carrying the ``child-failed:`` pause tag — terminal on
    request (the gr575883 carve-out: a done parent's stale tags gate
    nothing)."""
    ref = store.insert_ref(kind="todo", slug=None, title="paused parent", meta={})
    store.add_tag(ref.id, Tag.open("child-failed:1"), set_by="agent")
    if terminal:
        store.add_tag(ref.id, Tag.closed("STATUS", "done"), set_by="agent")
    return ref.id


def _job_under(
    store: Store,
    parent_id: int,
    *,
    executor: str,
    status: str = "queued",
    lane: str | None = None,
    params_lane: str | None = None,
) -> int:
    meta: dict[str, object] = {"job_type": "fake", "executor": executor}
    if lane is not None:
        meta["lane"] = lane
    if params_lane is not None:
        meta["params"] = {"lane": params_lane}
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="t",
        parent_id=parent_id,
        meta=meta,
    )
    store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="agent")
    return ref.id


def test_paused_parent_queue_is_not_dark_on_gated_executor(
    store: Store,
) -> None:
    """ssh_node's claim binds ``parent_not_paused``: a queue made only of
    jobs behind a paused parent has no claimable work — the lane reads
    neither dark nor claimer-less (the sweeper's parent-pause alert owns
    the wait)."""
    parent = _paused_parent_todo(store)
    job = _job_under(store, parent, executor="ssh_node")
    _age_queued_tag(store, job, 1200)
    body = _body(store, job)
    assert "lane alert" not in body


def test_terminal_parent_stale_pause_tag_keeps_dark(store: Store) -> None:
    """gr575883 carve-out: a ``done`` parent's leftover ``child-failed:``
    tag gates nothing, so the job IS claimable — the dark read fires."""
    parent = _paused_parent_todo(store, terminal=True)
    job = _job_under(store, parent, executor="ssh_node")
    _age_queued_tag(store, job, 1200)
    assert "lane alert: 'ssh_node' reads dark" in _body(store, job)


def test_mixed_queue_still_dark_on_claimable_head(store: Store) -> None:
    """A paused sibling does not hide real backlog: the dark count names
    only the claimable row."""
    parent = _paused_parent_todo(store)
    gated = _job_under(store, parent, executor="ssh_node")
    _age_queued_tag(store, gated, 3600)
    job = _job(store, "queued", executor="ssh_node")
    _age_queued_tag(store, job, 1200)
    assert "lane alert: 'ssh_node' reads dark — 1 claimable" in _body(store, job)


def test_paused_parent_counts_on_ungated_executor(store: Store) -> None:
    """job_inproc's claim does not bind the parent gate — the same row
    there IS claimable work, so an unclaimed one still reads dark."""
    parent = _paused_parent_todo(store)
    job = _job_under(store, parent, executor="job_inproc")
    _age_queued_tag(store, job, 1200)
    assert "lane alert: 'job_inproc' reads dark" in _body(store, job)


# ── parked-lane attribution is scoped to related lanes (review td677026)
#
# A ``lane-parked:<lane>`` alert may blame an executor only when the lane
# demonstrably feeds it: the lane's adapter is the executor's own
# credential family (``claude_p`` → ``claude_inproc``/``claude_docker``)
# or a claimable job queued on the executor names the lane. Anything
# else is an unrelated credential outage and must not attach.


def _park_alert(store: Store, lane: str) -> int:
    from precis.alerts import raise_alert

    aid, _ = raise_alert(
        store,
        source="lane_health",
        fingerprint=f"lane-parked:{lane}",
        title=f"[lane_health] {lane}: lane parked on failed health probe",
        detail="seeded",
        severity="warn",
    )
    return aid


def test_related_parked_lane_is_attributed(store: Store) -> None:
    """gr592838's own shape: the ``claude-sub`` account sits on adapter
    ``claude_p`` — the same subscription ``claude_inproc``'s subprocess
    draws on — so its park names the dark claude lane."""
    job = _job(store, "queued", executor="claude_inproc")
    _age_queued_tag(store, job, 1200)
    aid = _park_alert(store, "claude-sub")
    body = _body(store, job)
    assert "lane alert: 'claude_inproc' reads dark" in body
    assert f"al{aid} lane_health/lane-parked:claude-sub" in body


def test_unrelated_parked_lane_is_not_attributed(store: Store) -> None:
    """The ``kimi`` account is adapter ``kimi_tree`` — nothing on
    ``claude_inproc`` draws on it, so its park must not hang on the dark
    claude lane."""
    job = _job(store, "queued", executor="claude_inproc")
    _age_queued_tag(store, job, 1200)
    _park_alert(store, "kimi")
    body = _body(store, job)
    assert "lane alert: 'claude_inproc' reads dark" in body
    assert "lane-parked" not in body


def test_parked_lane_attributed_by_queue_evidence(store: Store) -> None:
    """A claimable job on the dark executor that names the lane
    (``params.lane`` pin — ``meta.lane`` is the same evidence) makes the
    relation itself: a ``kimi`` slice queued on ``ssh_node`` is exactly
    what a parked kimi lane explains."""
    job = _job(store, "queued", executor="ssh_node", params={"lane": "kimi"})
    _age_queued_tag(store, job, 1200)
    aid = _park_alert(store, "kimi")
    body = _body(store, job)
    assert "lane alert: 'ssh_node' reads dark" in body
    assert f"al{aid} lane_health/lane-parked:kimi" in body


def test_parked_lane_without_evidence_is_not_attributed(
    store: Store,
) -> None:
    """No job on ``ssh_node`` names the parked lane and no adapter map
    covers it — the outage stays unattributed rather than guessed."""
    job = _job(store, "queued", executor="ssh_node")
    _age_queued_tag(store, job, 1200)
    _park_alert(store, "kimi")
    body = _body(store, job)
    assert "lane alert: 'ssh_node' reads dark" in body
    assert "lane-parked" not in body


def test_gated_jobs_are_not_queue_evidence_for_parked_lane(
    store: Store,
) -> None:
    """Review td677026 second pass on gr592838: on ``ssh_node`` — a
    parent-gated executor — a queued job behind a paused parent is not
    claimable, so neither its ``meta.lane`` nor its ``params.lane`` can
    be the evidence that ties a parked lane to this executor. The mixed
    queue (old paused kimi jobs + unrelated eligible backlog) still
    reads dark on the eligible row, but kimi's park stays
    unattributed."""
    parent = _paused_parent_todo(store)
    gated_a = _job_under(store, parent, executor="ssh_node", lane="kimi")
    gated_b = _job_under(store, parent, executor="ssh_node", params_lane="kimi")
    _age_queued_tag(store, gated_a, 7200)
    _age_queued_tag(store, gated_b, 7200)
    eligible = _job(store, "queued", executor="ssh_node")
    _age_queued_tag(store, eligible, 1200)
    _park_alert(store, "kimi")
    body = _body(store, eligible)
    assert "lane alert: 'ssh_node' reads dark" in body
    assert "lane-parked" not in body


def test_gated_job_is_evidence_on_ungated_executor(store: Store) -> None:
    """The gate binds the executor's claim, not the tag: ``job_inproc``'s
    claim ignores the parent gate, so the same paused-parent row there IS
    claimable — and a lane it names remains queue evidence for the
    park."""
    parent = _paused_parent_todo(store)
    job = _job_under(store, parent, executor="job_inproc", params_lane="kimi")
    _age_queued_tag(store, job, 1200)
    aid = _park_alert(store, "kimi")
    body = _body(store, job)
    assert "lane alert: 'job_inproc' reads dark" in body
    assert f"al{aid} lane_health/lane-parked:kimi" in body
