"""Tests for the Slice-5 dispatch worker (``workers/dispatch.py``).

Covers candidate enumeration, the FOR UPDATE SKIP LOCKED claim,
child job minting with the right meta, auto_check auto-injection,
and the rejection paths (unknown executor / job_type / incompatible
combo).
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.todo import TodoHandler
from precis.store import Store
from precis.workers.dispatch import _candidate_parent_ids, run_dispatch_pass
from tests.conftest import id_of


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


def _child_jobs_under(store: Store, parent_id: int) -> list[dict]:
    """Fetch metadata for every kind='job' child of ``parent_id``."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, title, meta FROM refs "
            "WHERE parent_id = %s AND kind = 'job' AND retired_at IS NULL "
            "ORDER BY ref_id",
            (parent_id,),
        ).fetchall()
    return [{"id": int(r[0]), "title": r[1], "meta": r[2]} for r in rows]


# ── candidate enumeration ────────────────────────────────────────


def test_no_executor_no_dispatch(handler: TodoHandler, store: Store) -> None:
    """A todo without meta.executor is not a candidate."""
    handler.put(text="plain todo, no executor")
    result = run_dispatch_pass(store)
    assert result.claimed == 0
    assert result.ok == 0


def test_skips_when_child_job_exists(handler: TodoHandler, store: Store) -> None:
    """Once a child job exists, no further dispatch (bubble-up rule)."""
    r = handler.put(
        text="dispatchable",
        meta={
            "executor": "claude_inproc",
            "job_type": "fix_gripe",
            "params": {},
        },
    )
    rid = id_of(r.body)
    # Pre-seed a child job so the dispatcher should skip.
    store.insert_ref(kind="job", slug=None, title="prior", meta={}, parent_id=rid)
    result = run_dispatch_pass(store)
    assert result.claimed == 0
    # Still only the pre-seeded one.
    assert len(_child_jobs_under(store, rid)) == 1


def test_skips_paused_parent(handler: TodoHandler, store: Store) -> None:
    r = handler.put(
        text="paused",
        meta={"executor": "claude_inproc", "job_type": "fix_gripe"},
    )
    rid = id_of(r.body)
    from precis.store.types import Tag

    store.add_tag(
        rid, Tag.closed("STATUS", "paused"), set_by="agent", replace_prefix=True
    )
    result = run_dispatch_pass(store)
    assert result.claimed == 0
    assert _child_jobs_under(store, rid) == []


def test_skips_done_parent(handler: TodoHandler, store: Store) -> None:
    r = handler.put(
        text="done",
        meta={"executor": "claude_inproc", "job_type": "fix_gripe"},
    )
    rid = id_of(r.body)
    handler.tag(id=rid, add=["STATUS:done"])
    result = run_dispatch_pass(store)
    assert result.claimed == 0


def test_skips_halted_parent(handler: TodoHandler, store: Store) -> None:
    """Halt tag on the parent must keep the dispatcher off it.

    Same registry as ``view='doable'``: ``halt`` belongs in
    ``_DOABLE_EXCLUSION_TAGS`` and both surfaces honour it.
    """
    from precis.store.types import Tag

    r = handler.put(
        text="halted dispatch target",
        meta={"executor": "claude_inproc", "job_type": "fix_gripe"},
    )
    rid = id_of(r.body)
    store.add_tag(rid, Tag.open("halt"), set_by="user")
    result = run_dispatch_pass(store)
    assert result.claimed == 0
    assert _child_jobs_under(store, rid) == []


# ── happy path ───────────────────────────────────────────────────


def test_mints_child_job_under_parent(handler: TodoHandler, store: Store) -> None:
    r = handler.put(
        text="ready to dispatch",
        meta={
            "executor": "claude_inproc",
            "job_type": "fix_gripe",
            "params": {"key": "value"},
        },
    )
    rid = id_of(r.body)
    result = run_dispatch_pass(store)
    assert result.claimed == 1
    assert result.ok == 1
    children = _child_jobs_under(store, rid)
    assert len(children) == 1
    child = children[0]
    assert child["meta"]["job_type"] == "fix_gripe"
    assert child["meta"]["executor"] == "claude_inproc"
    assert child["meta"]["dispatched_from_todo"] == rid
    assert child["meta"]["params"] == {"key": "value"}
    # The child has STATUS:queued.
    tags = {str(t) for t in store.tags_for(child["id"])}
    assert "STATUS:queued" in tags
    # A dispatch event was appended on the parent.
    events = store.events_for(rid)
    assert any(e.event == "job-minted" and e.source == "minter" for e in events)


def test_auto_injects_auto_check_when_missing(
    handler: TodoHandler, store: Store
) -> None:
    """Parent didn't write meta.auto_check → dispatcher injects default."""
    r = handler.put(
        text="needs auto-check",
        meta={"executor": "claude_inproc", "job_type": "fix_gripe"},
    )
    rid = id_of(r.body)
    run_dispatch_pass(store)
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None
    assert ref.meta.get("auto_check") == {"type": "child_job_succeeded"}


def test_preserves_existing_auto_check(handler: TodoHandler, store: Store) -> None:
    """Caller-supplied auto_check survives dispatch unchanged."""
    custom = {"type": "time_past", "at": "2099-01-01T00:00:00+00:00"}
    r = handler.put(
        text="explicit auto-check",
        meta={
            "executor": "claude_inproc",
            "job_type": "fix_gripe",
            "auto_check": custom,
        },
    )
    rid = id_of(r.body)
    run_dispatch_pass(store)
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None
    assert ref.meta.get("auto_check") == custom


def test_plan_tick_parent_gets_no_auto_check(
    handler: TodoHandler, store: Store
) -> None:
    """A meta.llm_tier-set (plan_tick) parent must NOT get child_job_succeeded.

    The planner coroutine drives its own STATUS; a clean tick exits
    STATUS:succeeded even when it yielded or minted children. Injecting
    child_job_succeeded would auto-close the parent on its first tick.
    """
    r = handler.put(text="planner brief", meta={"llm_tier": "opus"})
    rid = id_of(r.body)
    run_dispatch_pass(store)
    children = _child_jobs_under(store, rid)
    assert len(children) == 1
    assert children[0]["meta"]["job_type"] == "plan_tick"
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None
    assert "auto_check" not in ref.meta


def test_plan_tick_parent_strips_stale_child_job_succeeded(
    handler: TodoHandler, store: Store
) -> None:
    """A planner parent carrying a stale child_job_succeeded auto_check
    has it STRIPPED on dispatch.

    Declining to inject (test above) isn't enough when a legacy /
    hand-authored spec is already attached — that's exactly what
    auto-closed an in-progress paper cascade on its first clean tick.
    """
    r = handler.put(
        text="planner brief with stale footgun",
        meta={"llm_tier": "opus", "auto_check": {"type": "child_job_succeeded"}},
    )
    rid = id_of(r.body)
    run_dispatch_pass(store)
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None
    assert "auto_check" not in ref.meta


def test_plan_tick_parent_keeps_non_footgun_auto_check(
    handler: TodoHandler, store: Store
) -> None:
    """Only the footgun type is stripped — a deliberate non-job auto_check
    on a planner survives."""
    custom = {"type": "time_past", "at": "2099-01-01T00:00:00+00:00"}
    r = handler.put(
        text="planner with deliberate timer",
        meta={"llm_tier": "opus", "auto_check": custom},
    )
    rid = id_of(r.body)
    run_dispatch_pass(store)
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None
    assert ref.meta.get("auto_check") == custom


def test_plan_tick_synthesizes_model_with_explicit_executor(
    handler: TodoHandler, store: Store
) -> None:
    """meta.llm_tier still supplies params['model'] even when the filer set
    meta.executor/job_type explicitly (the precis-job-help canonical
    pattern) instead of relying on the NULL-executor synthesis path.

    Regression: dispatch used to only synthesize ``model`` when
    ``executor`` was left unset, so this exact combo minted a plan_tick
    job with no ``model`` param and it crashed instantly on
    ``params['model']`` (KeyError).
    """
    r = handler.put(
        text="planner brief with explicit executor",
        meta={
            "llm_tier": "sonnet",
            "executor": "claude_inproc",
            "job_type": "plan_tick",
            "params": {},
        },
    )
    rid = id_of(r.body)
    run_dispatch_pass(store)
    children = _child_jobs_under(store, rid)
    assert len(children) == 1
    assert children[0]["meta"]["job_type"] == "plan_tick"
    assert children[0]["meta"]["params"]["model"] == "sonnet"


def test_plan_tick_threads_llm_select_into_job_params(
    handler: TodoHandler, store: Store
) -> None:
    """``meta.llm_select`` (structured selection, an optional
    sibling of ``llm_tier``) rides along onto the minted job's
    ``params['select']`` unchanged."""
    select = {"placement": "local", "thinking": True, "effort": "high"}
    r = handler.put(
        text="planner brief with structured selection",
        meta={"llm_tier": "sonnet", "llm_select": select},
    )
    rid = id_of(r.body)
    run_dispatch_pass(store)
    children = _child_jobs_under(store, rid)
    assert len(children) == 1
    assert children[0]["meta"]["params"]["model"] == "sonnet"
    assert children[0]["meta"]["params"]["select"] == select


def test_plan_tick_no_llm_select_omits_select_param(
    handler: TodoHandler, store: Store
) -> None:
    """A planner todo with no ``meta.llm_select`` mints a job with no
    ``params['select']`` at all — untouched, byte-identical to before this
    knob existed."""
    r = handler.put(text="planner brief, no selection", meta={"llm_tier": "sonnet"})
    rid = id_of(r.body)
    run_dispatch_pass(store)
    children = _child_jobs_under(store, rid)
    assert len(children) == 1
    assert "select" not in children[0]["meta"]["params"]


# ── served-model affinity: llm: requires stamped on a served-OSS plan_tick ──
#
# `_served_model_requirement` gates a minted `plan_tick` child onto a host
# serving its rung-0 OSS model — but only when that model is actually
# advertised as an `llm:` resource_slots row somewhere. A cloud/claude
# rung-0, or a model served nowhere, must mint host-agnostic (no requires).


def test_plan_tick_served_model_stamps_llm_requires(
    handler: TodoHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.utils.llm import router

    monkeypatch.setattr(
        router, "planner_rung0_model", lambda alias, job_type=None: "qwen-served-x"
    )
    store.sync_host_resource_slots("melchior", {"llm:qwen-served-x": 1})

    r = handler.put(text="planner brief, served model", meta={"llm_tier": "local"})
    rid = id_of(r.body)
    run_dispatch_pass(store)

    children = _child_jobs_under(store, rid)
    assert len(children) == 1
    assert children[0]["meta"]["requires"] == {"llm:qwen-served-x": 1}


def test_plan_tick_cloud_model_gets_no_requires(
    handler: TodoHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rung-0 resolves to a claude/cloud model → planner_rung0_model returns
    None → no requires key at all (host-agnostic, byte-identical to before
    this affinity gate existed)."""
    from precis.utils.llm import router

    monkeypatch.setattr(
        router, "planner_rung0_model", lambda alias, job_type=None: None
    )

    r = handler.put(text="planner brief, cloud model", meta={"llm_tier": "opus"})
    rid = id_of(r.body)
    run_dispatch_pass(store)

    children = _child_jobs_under(store, rid)
    assert len(children) == 1
    assert "requires" not in children[0]["meta"]


def test_plan_tick_served_model_absent_from_resource_slots_gets_no_requires(
    handler: TodoHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resolved model isn't advertised as an ``llm:`` slot anywhere (not
    seeded on any host) — gate stays dark rather than stamping an
    unsatisfiable requirement that would strand the job unclaimed forever."""
    from precis.utils.llm import router

    monkeypatch.setattr(
        router, "planner_rung0_model", lambda alias, job_type=None: "qwen-nowhere"
    )
    # deliberately no store.sync_host_resource_slots(...) for llm:qwen-nowhere

    r = handler.put(text="planner brief, unserved model", meta={"llm_tier": "local"})
    rid = id_of(r.body)
    run_dispatch_pass(store)

    children = _child_jobs_under(store, rid)
    assert len(children) == 1
    assert "requires" not in children[0]["meta"]


def test_non_plan_tick_job_never_gets_served_model_requires(
    handler: TodoHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The narrow gate is plan_tick-only — even if the resolver would happily
    return a served model, a non-plan_tick job_type is never touched."""
    from precis.utils.llm import router

    monkeypatch.setattr(
        router, "planner_rung0_model", lambda alias, job_type=None: "qwen-served-x"
    )
    store.sync_host_resource_slots("melchior", {"llm:qwen-served-x": 1})

    r = handler.put(
        text="a fix_gripe job, not a planner tick",
        meta={"executor": "claude_inproc", "job_type": "fix_gripe", "params": {}},
    )
    rid = id_of(r.body)
    run_dispatch_pass(store)

    children = _child_jobs_under(store, rid)
    assert len(children) == 1
    assert children[0]["meta"]["job_type"] == "fix_gripe"
    assert "requires" not in children[0]["meta"]


def test_succeeded_child_job_does_not_block_redispatch(
    handler: TodoHandler, store: Store
) -> None:
    """A terminal STATUS:succeeded job is a completed prior tick, not a
    live one — the planner parent must remain re-dispatchable."""
    from precis.store.types import Tag
    from precis.workers.dispatch import _candidate_parent_ids

    parent = handler.put(text="planner", meta={"llm_tier": "opus"})
    pid = id_of(parent.body)
    job = store.insert_ref(
        kind="job", slug=None, title="prior tick", meta={}, parent_id=pid
    )
    store.add_tag(
        job.id, Tag.closed("STATUS", "succeeded"), set_by="system", replace_prefix=True
    )
    assert pid in _candidate_parent_ids(store, limit=10)


def test_succeeded_child_job_blocks_deterministic_parent(
    handler: TodoHandler, store: Store
) -> None:
    """gr192606: a DETERMINISTIC parent (meta.executor + child_job_succeeded
    auto_check, no llm_tier) whose child job has STATUS:succeeded must NOT be
    re-dispatched. The succeeded child is the finished work; auto_check flips
    the parent STATUS:done on its next sweep. Re-minting in that gap was the
    runaway (46 jobs/23h for the daily 'briefing' todo, each destructively
    replacing the briefing-<date> news ref) — the mirror image of the planner
    test above, which stays re-dispatchable because it self-resolves."""
    from precis.store.types import Tag
    from precis.workers.dispatch import _candidate_parent_ids

    parent = handler.put(
        text="deterministic briefing",
        meta={"executor": "claude_inproc", "job_type": "fix_gripe", "params": {}},
    )
    pid = id_of(parent.body)
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="succeeded briefing job",
        meta={"job_type": "fix_gripe"},
        parent_id=pid,
    )
    store.add_tag(
        job.id, Tag.closed("STATUS", "succeeded"), set_by="system", replace_prefix=True
    )
    # Not a candidate, and a full pass mints nothing new.
    assert pid not in _candidate_parent_ids(store, limit=10)
    result = run_dispatch_pass(store)
    assert result.ok == 0
    assert len(_child_jobs_under(store, pid)) == 1


def test_succeeded_plan_tick_child_exempts_non_tier_parent(
    handler: TodoHandler, store: Store
) -> None:
    """Belt-and-suspenders: even without meta.llm_tier, a succeeded child whose
    job_type is self-resolving (``plan_tick``) does NOT block — the coroutine
    drives its own re-ticking regardless of how its parent is tagged."""
    from precis.store.types import Tag
    from precis.workers.dispatch import _candidate_parent_ids

    parent = handler.put(
        text="explicit plan_tick, no llm_tier",
        meta={"executor": "claude_inproc", "job_type": "plan_tick", "params": {}},
    )
    pid = id_of(parent.body)
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="prior plan_tick",
        meta={"job_type": "plan_tick"},
        parent_id=pid,
    )
    store.add_tag(
        job.id, Tag.closed("STATUS", "succeeded"), set_by="system", replace_prefix=True
    )
    assert pid in _candidate_parent_ids(store, limit=10)


def test_running_child_job_blocks_redispatch(
    handler: TodoHandler, store: Store
) -> None:
    """A non-terminal (running) job is in-flight and DOES block — guards
    against the dispatcher double-minting while a tick is live."""
    from precis.store.types import Tag
    from precis.workers.dispatch import _candidate_parent_ids

    parent = handler.put(text="planner", meta={"llm_tier": "opus"})
    pid = id_of(parent.body)
    job = store.insert_ref(
        kind="job", slug=None, title="in flight", meta={}, parent_id=pid
    )
    store.add_tag(
        job.id, Tag.closed("STATUS", "running"), set_by="system", replace_prefix=True
    )
    assert pid not in _candidate_parent_ids(store, limit=10)


def test_recurring_watch_root_excluded_from_candidates(
    handler: TodoHandler, store: Store
) -> None:
    """A recurring (``meta.schedule`` set) watch root is NEVER a dispatch
    candidate, even with an executor, an open STATUS, and no live child
    job/todo.

    Regression guard for the prod spin: the schedule worker (not the
    dispatcher) owns recurring cadence, spawning an ordinary
    worker-mintable subtask child each tick. Without this exclusion, a
    recurring root whose latest child resolves instantly satisfies every
    other eligibility clause and gets re-minted on every dispatch pass —
    ~1 job/5s on news_poll."""
    from precis.workers.dispatch import _candidate_parent_ids

    r = handler.put(
        text="news_poll cron root",
        meta={
            "executor": "claude_inproc",
            "job_type": "news_poll",
            "schedule": {"cron": "*/30 * * * *"},
        },
    )
    rid = id_of(r.body)
    assert rid not in _candidate_parent_ids(store, limit=10)


def test_non_recurring_parent_still_a_candidate(
    handler: TodoHandler, store: Store
) -> None:
    """Control for the recurring-root exclusion above: the identical
    parent WITHOUT ``meta.schedule`` set IS returned — guards against
    over-exclusion (e.g. matching on executor/job_type instead of the
    schedule presence)."""
    from precis.workers.dispatch import _candidate_parent_ids

    r = handler.put(
        text="news_poll cron root, untagged",
        meta={"executor": "claude_inproc", "job_type": "news_poll"},
    )
    rid = id_of(r.body)
    assert rid in _candidate_parent_ids(store, limit=10)


# ── rejection paths ──────────────────────────────────────────────


def test_skips_unknown_executor(handler: TodoHandler, store: Store) -> None:
    r = handler.put(
        text="bad executor",
        meta={"executor": "imaginary", "job_type": "fix_gripe"},
    )
    rid = id_of(r.body)
    result = run_dispatch_pass(store)
    assert result.claimed == 1
    assert result.ok == 0
    assert result.failed == 1
    assert _child_jobs_under(store, rid) == []


def _open_tag_values(store: Store, ref_id: int) -> set[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE rt.ref_id = %s AND t.namespace = 'OPEN'",
            (ref_id,),
        ).fetchall()
    return {str(r[0]) for r in rows}


def _backdate(store: Store, ref_id: int, hours: float) -> None:
    """Move ``refs.created_at`` backwards, for cooldown-window tests."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - %s::interval WHERE ref_id = %s",
            (f"{hours} hours", ref_id),
        )


# ── parked-child replan bypass (cooldown) ───────────────────────────


def _mint_planner_with_parked_child(
    store: Store, handler: TodoHandler, *, child_tags: list[str]
) -> tuple[int, int]:
    """Mint a meta.llm_tier=.opus. parent, a not-done child todo carrying every tag
    in ``child_tags``, and a succeeded ``plan_tick`` job child (freshly
    created — cooldown baseline). Returns ``(parent_id, job_id)``.
    """
    from precis.store.types import Tag

    parent = handler.put(text="planner with parked child", meta={"llm_tier": "opus"})
    pid = id_of(parent.body)
    child = store.insert_ref(
        kind="todo", slug=None, title="parked child", meta={}, parent_id=pid
    )
    for t in child_tags:
        store.add_tag(child.id, Tag.open(t), set_by="agent")
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="prior tick",
        meta={"job_type": "plan_tick"},
        parent_id=pid,
    )
    store.add_tag(
        job.id, Tag.closed("STATUS", "succeeded"), set_by="system", replace_prefix=True
    )
    return pid, job.id


def test_ask_user_child_blocks_redispatch_during_cooldown(
    handler: TodoHandler, store: Store
) -> None:
    """A parent parked only on an ask-user child is NOT re-dispatched
    right after its last plan_tick job — the cooldown holds."""
    from precis.workers.dispatch import _candidate_parent_ids

    pid, _job_id = _mint_planner_with_parked_child(
        store, handler, child_tags=["ask-user:please read this paper"]
    )
    assert pid not in _candidate_parent_ids(store, limit=10)
    result = run_dispatch_pass(store)
    assert result.claimed == 0
    # Only the pre-seeded succeeded tick — no new job minted.
    assert len(_child_jobs_under(store, pid)) == 1


def test_ask_user_child_allows_redispatch_after_cooldown(
    handler: TodoHandler, store: Store
) -> None:
    """The same parent IS re-dispatched once the cooldown window has
    elapsed since its last plan_tick job."""
    from precis.workers.dispatch import _candidate_parent_ids

    pid, job_id = _mint_planner_with_parked_child(
        store, handler, child_tags=["ask-user:please read this paper"]
    )
    _backdate(store, job_id, hours=7)  # past the 6h _PARKED_CHILD_REPLAN_COOLDOWN
    assert pid in _candidate_parent_ids(store, limit=10)
    result = run_dispatch_pass(store)
    assert result.claimed == 1
    assert result.ok == 1
    children = _child_jobs_under(store, pid)
    # The pre-seeded succeeded tick plus the freshly dispatched one.
    assert len(children) == 2


def test_waiting_for_child_allows_redispatch_after_cooldown(
    handler: TodoHandler, store: Store
) -> None:
    """``waiting-for:`` gets the same cooldown bypass as ``ask-user:``."""
    from precis.workers.dispatch import _candidate_parent_ids

    pid, job_id = _mint_planner_with_parked_child(
        store, handler, child_tags=["waiting-for:reto"]
    )
    _backdate(store, job_id, hours=7)
    assert pid in _candidate_parent_ids(store, limit=10)


def test_normal_open_child_still_blocks_redispatch_regardless_of_cooldown(
    handler: TodoHandler, store: Store
) -> None:
    """A genuinely in-flight (non-parked) open child todo must never be
    bypassed by the cooldown — unchanged existing behaviour, regression
    guard against loosening the gate too far."""
    from precis.workers.dispatch import _candidate_parent_ids

    parent = handler.put(text="planner with live child", meta={"llm_tier": "opus"})
    pid = id_of(parent.body)
    store.insert_ref(
        kind="todo", slug=None, title="still working", meta={}, parent_id=pid
    )
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="prior tick",
        meta={"job_type": "plan_tick"},
        parent_id=pid,
    )
    from precis.store.types import Tag

    store.add_tag(
        job.id, Tag.closed("STATUS", "succeeded"), set_by="system", replace_prefix=True
    )
    _backdate(store, job.id, hours=7)
    assert pid not in _candidate_parent_ids(store, limit=10)
    result = run_dispatch_pass(store)
    assert result.claimed == 0


def test_halt_child_never_bypassed_by_cooldown(
    handler: TodoHandler, store: Store
) -> None:
    """A parent whose only open child is halt-tagged is NEVER
    re-dispatched by the new bypass, even past the cooldown window —
    ``halt:`` stays a true hard stop, distinct from ``ask-user:``."""
    from precis.workers.dispatch import _candidate_parent_ids

    pid, job_id = _mint_planner_with_parked_child(store, handler, child_tags=["halt"])
    _backdate(store, job_id, hours=7)
    assert pid not in _candidate_parent_ids(store, limit=10)
    result = run_dispatch_pass(store)
    assert result.claimed == 0


def test_halt_plus_ask_user_child_never_bypassed_by_cooldown(
    handler: TodoHandler, store: Store
) -> None:
    """A child carrying BOTH ``halt`` and ``ask-user:`` at once — e.g.
    the planner escalates an already-parked child by adding ``halt:``
    without first removing ``ask-user:``, per the ``planner_prompt.py``
    escalation guidance — must stay a hard block. The hard-block tag
    wins over the bypass tag regardless of how long the cooldown has
    elapsed. Regression guard for the dual-tag gap the reviewer found:
    the original bypass check only asked "is a bypass tag present?"
    and never checked "is a hard-block tag ALSO present?", so a
    halted-and-parked child would wrongly stop blocking once 6h had
    passed."""
    from precis.workers.dispatch import _candidate_parent_ids

    pid, job_id = _mint_planner_with_parked_child(
        store,
        handler,
        child_tags=["ask-user:please read this paper", "halt:escalated"],
    )
    _backdate(store, job_id, hours=7)
    assert pid not in _candidate_parent_ids(store, limit=10)
    result = run_dispatch_pass(store)
    assert result.claimed == 0
    assert len(_child_jobs_under(store, pid)) == 1


# ── auto-timeout child is terminal, not live (gr236586) ─────────────
#
# ``workers/auto_check.py`` resolves a parked child todo to
# ``STATUS:auto-timeout`` when its own wait condition times out. Before the
# fix, the child-liveness ``NOT IN (...)`` clause in both
# ``_candidate_parent_ids`` and ``_claim_and_dispatch`` only excluded
# ``done`` / ``won't-do`` from "live" — an auto-timed-out child kept
# counting as live forever, permanently wedging the parent (and the whole
# plan_tick coroutine) out of dispatch candidacy with no alert.


def test_auto_timeout_child_does_not_block_redispatch(
    handler: TodoHandler, store: Store
) -> None:
    """A parent whose only child todo is STATUS:auto-timeout (no ask-user /
    waiting-for tags) IS a dispatch candidate — auto-timeout is a terminal
    status, same as done/won't-do. This is the exact case that was broken
    before the fix (the child used to still read as "live")."""
    from precis.store.types import Tag
    from precis.workers.dispatch import _candidate_parent_ids

    parent = handler.put(text="planner with timed-out child", meta={"llm_tier": "opus"})
    pid = id_of(parent.body)
    child = store.insert_ref(
        kind="todo", slug=None, title="timed-out leaf", meta={}, parent_id=pid
    )
    store.add_tag(
        child.id,
        Tag.closed("STATUS", "auto-timeout"),
        set_by="system",
        replace_prefix=True,
    )
    assert pid in _candidate_parent_ids(store, limit=10)


def test_done_child_does_not_block_redispatch(
    handler: TodoHandler, store: Store
) -> None:
    """Control / regression guard: a STATUS:done child already didn't block
    before this fix — must keep not blocking after it. Guards against a
    regressive fix that accidentally tightened the terminal-status set
    instead of widening it."""
    from precis.store.types import Tag
    from precis.workers.dispatch import _candidate_parent_ids

    parent = handler.put(
        text="planner with a finished child", meta={"llm_tier": "opus"}
    )
    pid = id_of(parent.body)
    child = store.insert_ref(
        kind="todo", slug=None, title="finished leaf", meta={}, parent_id=pid
    )
    store.add_tag(
        child.id, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
    )
    assert pid in _candidate_parent_ids(store, limit=10)


def test_auto_timeout_child_with_ask_user_tag_still_does_not_block(
    handler: TodoHandler, store: Store
) -> None:
    """A child carrying BOTH ``STATUS:auto-timeout`` and a leftover
    ``OPEN:ask-user:`` tag (the auto-check resolved the wait but the
    original park tag was never cleaned up) still does NOT block the
    parent.

    The terminal-status filter (``STATUS NOT IN ('done', "won't-do",
    'auto-timeout')``) is ANDed *before* ``_parked_child_still_blocks_sql``
    runs — so once a child's STATUS is itself terminal, the EXISTS
    subquery's first predicate is already false and the parked/hard-block
    tag logic in ``_parked_child_still_blocks_sql`` (whose docstring's
    "hard block always wins" is about the *open*-child, non-terminal-status
    case) never even gets evaluated for this row. Terminal STATUS wins
    outright over any OPEN:* tag still attached to the child."""
    from precis.store.types import Tag
    from precis.workers.dispatch import _candidate_parent_ids

    parent = handler.put(
        text="planner with timed-out-but-still-tagged child", meta={"llm_tier": "opus"}
    )
    pid = id_of(parent.body)
    child = store.insert_ref(
        kind="todo",
        slug=None,
        title="timed-out leaf, stale tag",
        meta={},
        parent_id=pid,
    )
    store.add_tag(
        child.id,
        Tag.closed("STATUS", "auto-timeout"),
        set_by="system",
        replace_prefix=True,
    )
    store.add_tag(child.id, Tag.open("ask-user:please read this paper"), set_by="agent")
    assert pid in _candidate_parent_ids(store, limit=10)


def test_unknown_executor_halts_parent_and_stops_re_dispatch(
    handler: TodoHandler, store: Store
) -> None:
    """A mis-configured parent self-halts so it stops flooding logs.

    Regression guard: a bogus executor used to warn-and-skip on *every*
    sweep (the parent stayed a candidate forever). Now the first sweep
    tags ``halt:bad-dispatch`` and the second sweep no longer claims it.
    """
    r = handler.put(
        text="bad executor",
        meta={"executor": "plan_tick", "job_type": "plan_tick"},
    )
    rid = id_of(r.body)

    first = run_dispatch_pass(store)
    assert first.claimed == 1
    assert first.failed == 1
    assert "halt:bad-dispatch" in _open_tag_values(store, rid)

    # The halt tag drops it from candidacy: no re-claim, no re-warn.
    second = run_dispatch_pass(store)
    assert second.claimed == 0
    assert _child_jobs_under(store, rid) == []


def test_skips_unknown_job_type(handler: TodoHandler, store: Store) -> None:
    r = handler.put(
        text="bad job_type",
        meta={
            "executor": "claude_inproc",
            "job_type": "simulate_warp_drive",
        },
    )
    rid = id_of(r.body)
    result = run_dispatch_pass(store)
    assert result.claimed == 1
    assert result.ok == 0
    assert result.failed == 1
    assert _child_jobs_under(store, rid) == []


def test_skips_missing_job_type(handler: TodoHandler, store: Store) -> None:
    r = handler.put(
        text="executor without job_type",
        meta={"executor": "claude_inproc"},
    )
    rid = id_of(r.body)
    result = run_dispatch_pass(store)
    assert result.claimed == 1
    assert result.ok == 0


# ── failure-bubble ───────────────────────────────────────────────


def test_bubble_helper_tags_parent_on_job_failure(
    handler: TodoHandler, store: Store
) -> None:
    """The bubble helper tags the parent todo ``child-failed:<job_id>``."""
    from precis.handlers._job_bubble import bubble_job_failure

    r = handler.put(text="Parent")
    rid = id_of(r.body)
    job = store.insert_ref(
        kind="job", slug=None, title="failed job", meta={}, parent_id=rid
    )
    bubble_job_failure(store, job.id)
    tags = {str(t) for t in store.tags_for(rid)}
    assert f"child-failed:{job.id}" in tags


def test_bubble_helper_noop_for_orphan_job(store: Store) -> None:
    """A job without parent_id (legacy) doesn't crash the bubble."""
    from precis.handlers._job_bubble import bubble_job_failure

    job = store.insert_ref(kind="job", slug=None, title="orphan", meta={})
    # Should not raise.
    bubble_job_failure(store, job.id)


def test_infra_retry_bump_rolls_back_with_callers_transaction(
    handler: TodoHandler, store: Store
) -> None:
    """When the caller shares ``conn`` (the executor paths' pattern), the
    orphan-retry counter bump must live in the *same* transaction as the
    job-status write — a caller-side rollback (the status write "didn't
    happen") must roll the counter increment back with it, not leave it
    surviving on an independent connection."""
    from precis.handlers._job_bubble import bubble_job_failure
    from precis.store.types import Tag

    r = handler.put(text="parent")
    rid = id_of(r.body)
    job = store.insert_ref(
        kind="job", slug=None, title="orphaned", meta={}, parent_id=rid
    )
    store.add_tag(job.id, Tag.open("swept:claim-orphaned"), set_by="system")

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom), store.pool.connection() as conn:
        bubble_job_failure(store, job.id, conn=conn)
        raise _Boom()  # simulate the caller's own status write failing

    with store.pool.connection() as check_conn:
        row = check_conn.execute(
            "SELECT meta->'orphan_retry_count' FROM refs WHERE ref_id = %s", (rid,)
        ).fetchone()
    assert row is not None
    assert row[0] is None  # rolled back together with the caller's tx


def test_infra_retry_bump_commits_with_callers_transaction(
    handler: TodoHandler, store: Store
) -> None:
    """The positive case of the rollback test above: a clean exit of the
    caller's shared transaction commits the bump exactly once."""
    from precis.handlers._job_bubble import bubble_job_failure
    from precis.store.types import Tag

    r = handler.put(text="parent")
    rid = id_of(r.body)
    job = store.insert_ref(
        kind="job", slug=None, title="orphaned", meta={}, parent_id=rid
    )
    store.add_tag(job.id, Tag.open("swept:claim-orphaned"), set_by="system")

    with store.pool.connection() as conn:
        bubble_job_failure(store, job.id, conn=conn)

    with store.pool.connection() as check_conn:
        row = check_conn.execute(
            "SELECT meta->'orphan_retry_count' FROM refs WHERE ref_id = %s", (rid,)
        ).fetchone()
    assert row is not None
    assert row[0] == 1
    parent_tags = {str(t) for t in store.tags_for(rid)}
    assert f"child-failed:{job.id}" not in parent_tags  # under the retry cap


def test_job_handler_tag_bubbles_status_failed(
    handler: TodoHandler, store: Store
) -> None:
    """``JobHandler.tag(add=['STATUS:failed'])`` triggers the bubble."""
    from precis.dispatch import Hub
    from precis.handlers.job import JobHandler

    job_handler = JobHandler(hub=Hub(store=store, embedder=None))
    r = handler.put(text="parent")
    rid = id_of(r.body)
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="will fail",
        meta={"job_type": "fix_gripe", "executor": "claude_inproc"},
        parent_id=rid,
    )
    from precis.store.types import Tag

    store.add_tag(
        job.id,
        Tag.closed("STATUS", "queued"),
        set_by="agent",
        replace_prefix=True,
    )
    job_handler.tag(id=job.id, add=["STATUS:failed"])
    parent_tags = {str(t) for t in store.tags_for(rid)}
    assert f"child-failed:{job.id}" in parent_tags


def test_job_handler_tag_other_status_does_not_bubble(
    handler: TodoHandler, store: Store
) -> None:
    """Tagging STATUS:succeeded doesn't add child-failed."""
    from precis.dispatch import Hub
    from precis.handlers.job import JobHandler

    job_handler = JobHandler(hub=Hub(store=store, embedder=None))
    r = handler.put(text="parent")
    rid = id_of(r.body)
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="ok",
        meta={"job_type": "fix_gripe", "executor": "claude_inproc"},
        parent_id=rid,
    )
    from precis.store.types import Tag

    store.add_tag(
        job.id,
        Tag.closed("STATUS", "running"),
        set_by="agent",
        replace_prefix=True,
    )
    job_handler.tag(id=job.id, add=["STATUS:succeeded"])
    parent_tags = {str(t) for t in store.tags_for(rid)}
    assert not any(t.startswith("child-failed:") for t in parent_tags)


# ── content failures stay permanently blocked (infra-retry regression) ──


def test_content_failure_latches_immediately_and_stays_blocked_past_infra_window(
    handler: TodoHandler, store: Store
) -> None:
    """A content-class failure (``JobHandler.tag(add=['STATUS:failed'])``,
    no ``swept:claim-orphaned``) latches ``child-failed:`` on the very
    first failure — unlike the infra-class bounded-retry path, there is no
    grace window here. Regression guard: even after the infra-class retry
    window (``ORPHAN_RETRY_WINDOW_HOURS``) has fully elapsed, the parent
    must STILL be excluded from dispatch candidacy — a blind timer-based
    "unlatch after N hours" would wrongly re-arm a genuinely broken task."""
    from precis.handlers._job_bubble import ORPHAN_RETRY_WINDOW_HOURS
    from precis.handlers.job import JobHandler
    from precis.workers.dispatch import _candidate_parent_ids

    job_handler = JobHandler(hub=Hub(store=store, embedder=None))
    r = handler.put(
        text="content-broken planner",
        meta={"executor": "claude_inproc", "job_type": "fix_gripe", "params": {}},
    )
    rid = id_of(r.body)
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="will fail on real content",
        meta={"job_type": "fix_gripe", "executor": "claude_inproc"},
        parent_id=rid,
    )
    from precis.store.types import Tag

    store.add_tag(
        job.id, Tag.closed("STATUS", "queued"), set_by="agent", replace_prefix=True
    )
    job_handler.tag(id=job.id, add=["STATUS:failed"])

    job_tags = {str(t) for t in store.tags_for(job.id)}
    assert "swept:claim-orphaned" not in job_tags  # genuinely content-class
    parent_tags = {str(t) for t in store.tags_for(rid)}
    assert f"child-failed:{job.id}" in parent_tags
    assert rid not in _candidate_parent_ids(store, limit=50)

    # Advance the clock well past the infra-retry window — a content
    # failure has no such window at all, so this must change nothing.
    with store.pool.connection() as conn:
        conn.execute(
            """
            UPDATE ref_tags rt
               SET created_at = now() - %s::interval
              FROM tags t
             WHERE rt.tag_id = t.tag_id
               AND rt.ref_id = %s
               AND t.namespace = 'OPEN'
               AND t.value = %s
            """,
            (
                f"{ORPHAN_RETRY_WINDOW_HOURS + 24} hours",
                rid,
                f"child-failed:{job.id}",
            ),
        )
        conn.commit()

    parent_tags = {str(t) for t in store.tags_for(rid)}
    assert f"child-failed:{job.id}" in parent_tags
    assert rid not in _candidate_parent_ids(store, limit=50)


# ── infra:child-killed (parked-leaf-recovery) ──────────────────────


def test_infra_child_killed_does_not_latch_under_the_retry_cap(
    handler: TodoHandler, store: Store
) -> None:
    """A ``infra:child-killed`` failure — the subprocess-exit
    misclassification fix (docs/backlog/parked-leaf-recovery.md) — is
    treated exactly like ``swept:claim-orphaned``: under
    ``ORPHAN_RETRY_CAP`` it does NOT latch ``child-failed:`` on the
    parent. Mirrors
    ``test_infra_retry_bump_commits_with_callers_transaction``."""
    from precis.handlers._job_bubble import bubble_job_failure
    from precis.store.types import Tag

    r = handler.put(text="parent")
    rid = id_of(r.body)
    job = store.insert_ref(
        kind="job", slug=None, title="child-killed", meta={}, parent_id=rid
    )
    store.add_tag(job.id, Tag.open("infra:child-killed"), set_by="system")

    with store.pool.connection() as conn:
        bubble_job_failure(store, job.id, conn=conn)

    with store.pool.connection() as check_conn:
        row = check_conn.execute(
            "SELECT meta->'orphan_retry_count' FROM refs WHERE ref_id = %s", (rid,)
        ).fetchone()
    assert row is not None
    assert row[0] == 1
    parent_tags = {str(t) for t in store.tags_for(rid)}
    assert f"child-failed:{job.id}" not in parent_tags  # under the retry cap


def test_infra_child_killed_latches_past_the_cap(
    handler: TodoHandler, store: Store
) -> None:
    """Repeated ``infra:child-killed`` failures on the same parent, past
    ``ORPHAN_RETRY_CAP``, DO latch — the existing orphan-retry cap is the
    single bound, regardless of which infra tag triggered it."""
    from precis.handlers._job_bubble import ORPHAN_RETRY_CAP, bubble_job_failure
    from precis.store.types import Tag

    r = handler.put(text="repeatedly child-killed parent")
    rid = id_of(r.body)

    last_job_id: int | None = None
    for i in range(ORPHAN_RETRY_CAP):
        job = store.insert_ref(
            kind="job", slug=None, title="child-killed", meta={}, parent_id=rid
        )
        last_job_id = int(job.id)
        store.add_tag(job.id, Tag.open("infra:child-killed"), set_by="system")
        bubble_job_failure(store, job.id)
        if i < ORPHAN_RETRY_CAP - 1:
            parent_tags = {str(t) for t in store.tags_for(rid)}
            assert f"child-failed:{last_job_id}" not in parent_tags

    parent_tags = {str(t) for t in store.tags_for(rid)}
    assert f"child-failed:{last_job_id}" in parent_tags
    assert "halt:orphan-retry-cap" in parent_tags


# ── concurrency ──────────────────────────────────────────────────


def test_row_lock_serialises_concurrent_dispatch(
    handler: TodoHandler, store: Store
) -> None:
    """Holding the parent's row lock in tx A blocks dispatch in tx B."""
    r = handler.put(
        text="locked target",
        meta={"executor": "claude_inproc", "job_type": "fix_gripe"},
    )
    rid = id_of(r.body)

    holder = store.pool.getconn()
    try:
        holder.execute("BEGIN")
        row = holder.execute(
            "SELECT ref_id FROM refs WHERE ref_id = %s FOR UPDATE",
            (rid,),
        ).fetchone()
        assert row is not None
        # While held, a parallel dispatch sees the row as SKIPPED.
        result = run_dispatch_pass(store)
        assert result.claimed == 0
        assert _child_jobs_under(store, rid) == []
        holder.execute("COMMIT")
    finally:
        store.pool.putconn(holder)

    # After release, the next pass mints normally.
    result2 = run_dispatch_pass(store)
    assert result2.claimed == 1
    assert result2.ok == 1


# ── prio propagation (slice 6a) ──────────────────────────────────


def _job_prio(store: Store, job_id: int) -> int | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT prio FROM refs WHERE ref_id = %s", (job_id,)
        ).fetchone()
    assert row is not None
    return None if row[0] is None else int(row[0])


def test_minted_job_inherits_parent_prio(handler: TodoHandler, store: Store) -> None:
    """A parent todo's prio flows onto the minted job verbatim (6a) — this
    only checks the value propagates unchanged, not the claim direction
    (see ``test_claim_ordering.py`` for the 0014 ASC pin)."""
    r = handler.put(
        text="non-default-prio work",
        meta={"executor": "claude_inproc", "job_type": "fix_gripe", "params": {}},
        prio=9,
    )
    rid = id_of(r.body)
    result = run_dispatch_pass(store)
    assert result.ok == 1
    jobs = _child_jobs_under(store, rid)
    assert len(jobs) == 1
    assert _job_prio(store, jobs[0]["id"]) == 9


def test_minted_job_prio_null_when_parent_unset(
    handler: TodoHandler, store: Store
) -> None:
    """An unset parent prio stays NULL on the job → claim's COALESCE default."""
    r = handler.put(
        text="commodity work",
        meta={"executor": "claude_inproc", "job_type": "fix_gripe"},
    )
    rid = id_of(r.body)
    run_dispatch_pass(store)
    jobs = _child_jobs_under(store, rid)
    assert len(jobs) == 1
    assert _job_prio(store, jobs[0]["id"]) is None


# ── orphan subtrees (deleted ancestor) ───────────────────────────


def _dispatchable_child(store: Store, title: str, parent_id: int | None) -> int:
    ref = store.insert_ref(
        kind="todo",
        slug=None,
        title=title,
        meta={"executor": "claude_inproc", "job_type": "fix_gripe", "params": {}},
        parent_id=parent_id,
    )
    return int(ref.id)


def _soft_delete(store: Store, ref_id: int) -> None:
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET retired_at = now() WHERE ref_id = %s", (ref_id,))
        conn.commit()


def test_skips_candidate_under_deleted_ancestor(
    handler: TodoHandler, store: Store
) -> None:
    """Deleting a parent must stop its whole subtree dispatching.

    ``retired_at`` is not transitive, and the candidate query only
    checks the candidate's *own* flag — so before ``_drop_orphaned``
    an orphaned subtree kept ticking indefinitely under a parent
    nobody could see any more.
    """
    root = id_of(handler.put(text="project root").body)
    mid = _dispatchable_child(store, "section", root)
    leaf = _dispatchable_child(store, "leaf task", mid)
    _soft_delete(store, root)  # two levels up from the leaf

    assert _candidate_parent_ids(store, limit=50) == []
    assert run_dispatch_pass(store).ok == 0
    assert _child_jobs_under(store, leaf) == []


def test_dispatches_when_ancestors_are_live(handler: TodoHandler, store: Store) -> None:
    """Control: the same shape with nothing deleted still dispatches."""
    root = id_of(handler.put(text="project root").body)
    leaf = _dispatchable_child(store, "leaf task", root)

    assert leaf in _candidate_parent_ids(store, limit=50)
    assert run_dispatch_pass(store).ok == 1
    assert len(_child_jobs_under(store, leaf)) == 1


# ── daily cost ceiling: cadence work is exempt ───────────────────


def _cadence_tick(store: Store, title: str) -> int:
    """A watch carrying ``meta.schedule`` plus the dispatchable tick under it —
    the shape the recurring spawner produces (the cadence lives on the watch,
    never copied down onto the tick)."""
    watch = store.insert_ref(
        kind="todo",
        slug=None,
        title=f"{title} (watch)",
        meta={
            "schedule": {"cron": "0 7 * * *", "backfill_missed": False},
            "executor": "claude_inproc",
            "job_type": "fix_gripe",
        },
        parent_id=None,
    )
    return _dispatchable_child(store, title, int(watch.id))


def test_daily_ceiling_still_dispatches_cadence_work(
    handler: TodoHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tripped global ceiling pauses discretionary dispatch but NOT the
    scheduled cadences.

    The regression: the ceiling used to ``break`` the round outright, applying a
    guardrail built for open-ended planner coroutines to every candidate. On
    2026-08-07 that held the fleet over the ceiling from 07:00 UTC and the
    morning brief's tick sat six hours with no job minted — for a cast that
    costs about $0.05. A ceiling of 0 trips unconditionally (spend >= 0).
    """
    monkeypatch.setenv("PRECIS_DAILY_COST_CEILING", "0")
    tick = _cadence_tick(store, "cast watch: reading 2026-08-07")
    discretionary = _dispatchable_child(store, "exploratory work", None)

    run_dispatch_pass(store)

    assert len(_child_jobs_under(store, tick)) == 1, "cadence tick must dispatch"
    assert _child_jobs_under(store, discretionary) == [], (
        "discretionary work must still be paused by the ceiling"
    )


def test_daily_ceiling_exemption_is_off_when_the_ceiling_is_clear(
    handler: TodoHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: with headroom, both lanes dispatch — the exemption is not a
    reordering, it only decides who survives a *tripped* ceiling."""
    monkeypatch.setenv("PRECIS_DAILY_COST_CEILING", "1000000")
    tick = _cadence_tick(store, "cast watch: nidra 2026-08-07")
    discretionary = _dispatchable_child(store, "exploratory work", None)

    run_dispatch_pass(store)

    assert len(_child_jobs_under(store, tick)) == 1
    assert len(_child_jobs_under(store, discretionary)) == 1


def test_daily_ceiling_still_dispatches_zero_llm_compute(
    handler: TodoHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tripped global ceiling must not starve a compute-only mint.

    The ceiling is an LLM budget (it sums ``llm_call_log``); an ``ssh_node``
    parent (e.g. the pure-numpy ``autocatpath_aggregate`` rollup) can't spend
    against it. Regression: cadence-exempt quest ticks held the trailing-24h
    window over the ceiling permanently, so no aggregate minted for 29h
    (2026-08-16/17) while seed jobs — minted outside the dispatcher — kept
    succeeding.
    """
    monkeypatch.setenv("PRECIS_DAILY_COST_CEILING", "0")
    compute = store.insert_ref(
        kind="todo",
        slug=None,
        title="aggregate rollup",
        meta={"executor": "ssh_node", "job_type": "struct_relax", "params": {}},
        parent_id=None,
    )
    discretionary = _dispatchable_child(store, "exploratory work", None)

    run_dispatch_pass(store)

    assert len(_child_jobs_under(store, int(compute.id))) == 1, (
        "zero-LLM compute must dispatch through a tripped ceiling"
    )
    assert _child_jobs_under(store, discretionary) == [], (
        "LLM-lane discretionary work must still be paused by the ceiling"
    )


def test_daily_ceiling_zero_llm_exemption_vetoed_by_llm_tier(
    handler: TodoHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hybrid candidate (compute executor + ``meta.llm_tier``) stays
    discretionary — the exemption is strictly for mints that can't spend."""
    monkeypatch.setenv("PRECIS_DAILY_COST_CEILING", "0")
    hybrid = store.insert_ref(
        kind="todo",
        slug=None,
        title="hybrid planner-compute",
        meta={
            "executor": "ssh_node",
            "job_type": "struct_relax",
            "params": {},
            "llm_tier": "haiku",
        },
        parent_id=None,
    )

    run_dispatch_pass(store)

    assert _child_jobs_under(store, int(hybrid.id)) == [], (
        "llm_tier candidate must not ride the zero-LLM exemption"
    )
