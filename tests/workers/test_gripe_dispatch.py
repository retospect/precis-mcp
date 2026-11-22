"""gripe_dispatch: the dark factory's intake pass (td564019 cut 3)."""

from __future__ import annotations

from typing import Any

import pytest
from psycopg.types.json import Jsonb

from precis import settings as psettings
from precis.plans.lanes import ANY_CLASS, Lane
from precis.store import Store
from precis.store.types import Tag
from precis.workers import gripe_dispatch as gd
from tests._lane_rows import use_lanes
from tests._refs import insert_gripe

_AGENT = {"filed_by": {"actor": "agent", "client": "claude-code/2.1.308"}}
_HUMAN = {"filed_by": {"actor": "user:rs", "client": "web"}}
_WORKER = {"filed_by": {"actor": "agent", "source": "memory_gardener"}}


def _gripe(
    store: Store,
    title: str,
    *,
    status: str = "open",
    meta: dict[str, Any] | None = None,
    tags: tuple[str, ...] = ("fix:code",),
    age_minutes: float = 120.0,
    prio: int | None = None,
) -> int:
    ref = insert_gripe(store, title, status=status)
    for t in tags:
        store.add_tag(ref.id, Tag.open(t), set_by="agent")
    # ``insert_ref`` re-stamps ``filed_by`` from the live filing origin
    # (``filing_origin.stamp_meta``), so the provenance under test is
    # written afterwards, as the creation's own stamp would be.
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = %s, "
            "created_at = now() - make_interval(secs => %s), prio = %s "
            "WHERE ref_id = %s",
            (
                Jsonb(dict(_AGENT if meta is None else meta)),
                age_minutes * 60,
                prio,
                ref.id,
            ),
        )
    _age_dispatch_tags(store, int(ref.id), age_minutes)
    return int(ref.id)


def _age_dispatch_tags(store: Store, ref_id: int, minutes: float) -> None:
    """Model tags attached when the fixture gripe became dispatchable."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_tags rt SET created_at = now() - make_interval(secs => %s) "
            "FROM tags t WHERE rt.tag_id = t.tag_id AND rt.ref_id = %s "
            "AND ((t.namespace IN ('OPEN', 'FLAG') AND t.value = ANY(%s)) "
            "OR (t.namespace = 'STATUS' AND t.value = 'triaged'))",
            (minutes * 60, ref_id, list(gd.FIX_CLASS_TAGS)),
        )


def _tag_age(store: Store, ref_id: int, value: str, minutes: float) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_tags rt SET created_at = now() - make_interval(secs => %s) "
            "FROM tags t WHERE rt.tag_id = t.tag_id AND rt.ref_id = %s "
            "AND t.value = %s",
            (minutes * 60, ref_id, value),
        )


@pytest.fixture(autouse=True)
def _fresh_trust_cache() -> Any:
    """``factory.gripe_trust`` rides the 60 s settings cache; keep one
    test's value out of the next."""
    psettings.invalidate(gd.TRUST_KEY)
    yield
    psettings.invalidate(gd.TRUST_KEY)


def _trust(store: Store, mode: str) -> None:
    psettings.set_setting(gd.TRUST_KEY, mode, store=store)


def _light(store: Store, prio: int = 1, host: str = "*") -> None:
    # Legacy intake cases use a synthetic fleet with the same number of
    # free executor slots as their configured lanes, and one queued job
    # per slot. Queue-depth defaults and host caps have separate tests.
    psettings.set_setting("factory.gripe_dispatch.queue_depth", 1, store=store)
    store.sync_host_resource_slots("h1", {"builder": 1, "codex_bin": 1})
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO host_heartbeat (host, ts, meta) VALUES ('h1', now(), '{}') "
            "ON CONFLICT (host) DO UPDATE SET ts = now()"
        )
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES "
            "('claim.executor_cap.ssh_node.h1', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (str(gd.lane_capacity(store) + 1),),
        )
        conn.execute(
            "INSERT INTO service_config (host, service, prio) "
            "VALUES (%s, 'gripe_dispatch', %s) "
            "ON CONFLICT (host, service) DO UPDATE SET prio = EXCLUDED.prio",
            (host, prio),
        )


def _roots_for(store: Store, gripe_id: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT l.src_ref_id FROM links l JOIN refs t ON t.ref_id = l.src_ref_id "
            "WHERE l.dst_ref_id = %s AND l.relation = 'related-to' AND t.kind = 'todo' "
            "ORDER BY 1",
            (gripe_id,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _comments(store: Store, gripe_id: int) -> list[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND chunk_kind = 'gripe_comment' "
            "ORDER BY ord",
            (gripe_id,),
        ).fetchall()
    return [str(r[0]) for r in rows]


@pytest.fixture
def two_lanes(monkeypatch: pytest.MonkeyPatch) -> tuple[Lane, Lane]:
    codex = Lane(
        "codex", "codex_exec", "codex", 1, frozenset({"code"}), vendor="openai"
    )
    sub = Lane(
        "claude-sub",
        "claude_p",
        "sub",
        2,
        frozenset({ANY_CLASS}),
        vendor="anthropic",
        models=("claude-sonnet-5-5",),
        model_prefix="claude-",
    )
    use_lanes(monkeypatch, codex, sub)
    return codex, sub


def test_dark_without_service_config(store: Store, two_lanes: Any) -> None:
    _gripe(store, "leaks")
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert (res.claimed, res.ok, res.failed) == (0, 0, 0)
    assert not gd.gripe_dispatch_enabled(store, host="h1")


def test_lane_capacity_sums_enabled_code_fit_caps(two_lanes: Any) -> None:
    codex, sub = two_lanes
    assert gd.lane_capacity(None) == 3
    off = Lane("devin", "devin_tree", "devin", 4, frozenset({"code"}), enabled=False)
    review_only = Lane("rev", "claude_p", "sub", 9, frozenset({"review"}))
    assert gd.lane_capacity(None, (codex, off, review_only)) == 1


def test_candidates_filter_and_order(store: Store, two_lanes: Any) -> None:
    """``filer`` mode: today's rule (Claude Code / human / ci.sig only)."""
    _trust(store, "filer")
    hot = _gripe(store, "hot", prio=2)
    old = _gripe(store, "old", age_minutes=600)
    newer = _gripe(store, "newer", age_minutes=61)
    _gripe(store, "human", meta=_HUMAN, prio=1)
    _gripe(store, "too young", age_minutes=5)
    _gripe(store, "worker-filed", meta=_WORKER, prio=1)
    _gripe(store, "no provenance", meta={}, prio=1)
    _gripe(store, "not code", tags=("fix:infra",))
    _gripe(store, "held", tags=("fix:code", "hold:no-dispatch"))
    _gripe(store, "never-close", tags=("fix:code", "hold:no-autoclose"))
    _gripe(store, "reto's", tags=("fix:code", "fix:reto"))
    _gripe(store, "disputed", tags=("fix:code", "disagreement"))
    _gripe(store, "closed", status="done")
    _gripe(store, "fixing already", status="fixing")
    picks = gd.candidates(store, 10)
    titles = [c.title for c in picks]
    assert titles == ["human", "hot", "old", "newer"]
    assert [c.gripe_id for c in picks][1:] == [hot, old, newer]
    assert gd.candidates(store, 0) == []


def test_gripe_trust_default_is_all(store: Store) -> None:
    assert gd.gripe_trust(store) == "all"
    _trust(store, "filer")
    assert gd.gripe_trust(store) == "filer"
    _trust(store, " ALL ")
    assert gd.gripe_trust(store) == "all"
    _trust(store, "bogus")  # a typo tightens, never loosens
    assert gd.gripe_trust(store) == "filer"


def test_trust_all_skips_only_the_filer_check(store: Store, two_lanes: Any) -> None:
    """``all`` (the default): worker-filed and unstamped gripes dispatch;
    every other filter still applies."""
    assert gd.gripe_trust(store) == "all"
    _gripe(store, "agent", prio=1)
    _gripe(store, "worker-filed", meta=_WORKER, prio=2)
    _gripe(store, "no provenance", meta={}, prio=3)
    _gripe(store, "too young", meta=_WORKER, age_minutes=5, prio=9)
    _gripe(store, "held", meta=_WORKER, tags=("fix:code", "hold:no-dispatch"))
    _gripe(store, "never-close", meta=_WORKER, tags=("fix:code", "hold:no-autoclose"))
    _gripe(store, "reto's", meta=_WORKER, tags=("fix:code", "fix:reto"))
    _gripe(store, "not code", meta=_WORKER, tags=("fix:infra",))
    _gripe(store, "closed", meta=_WORKER, status="done")
    linked = _gripe(store, "already chained", meta=_WORKER, prio=9)
    gd.mint_gripe_fix_direct(store, gripe_id=linked)
    titles = [c.title for c in gd.candidates(store, 10)]
    assert titles == ["agent", "worker-filed", "no provenance"]

    _trust(store, "filer")
    assert [c.title for c in gd.candidates(store, 10)] == ["agent"]


@pytest.mark.parametrize(
    "tag", ["fix:code", "fix:skill", "fix:docs", "fix:web", "fix:mcp"]
)
def test_each_fix_class_mints_a_code_class_build(
    store: Store, two_lanes: Any, tag: str
) -> None:
    """Every fix class is a candidate and mints the same chain: the build
    carries no task_class override, so it resolves to ``code``."""
    _light(store)
    gid = _gripe(store, f"a {tag} gripe", tags=(tag,))
    assert [c.gripe_id for c in gd.candidates(store, 5)] == [gid]
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert (res.claimed, res.ok, res.failed) == (1, 1, 0)
    [root] = _roots_for(store, gid)
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT COALESCE(bn.meta->>'task_class', 'code') FROM refs bn "
            "JOIN ref_tags bpt ON bpt.ref_id = bn.ref_id "
            "JOIN tags bp ON bp.tag_id = bpt.tag_id "
            "AND bp.namespace = 'OPEN' AND bp.value = 'plan:' || %s::text "
            "WHERE bn.kind = 'todo' "
            "AND COALESCE(bn.meta->'op'->>'type', bn.meta->>'op') = ANY(%s)",
            (root, list(gd._BUILD_OPS)),
        ).fetchall()
    # one build (code) plus the validate run_slice; nothing is docs-classed
    assert sorted(r[0] for r in rows) == ["code", "validate"]
    # Minting a queued build consumes no running executor slot.
    assert gd.open_chain_count(store) == 0


def test_non_fix_classes_stay_out(store: Store, two_lanes: Any) -> None:
    _gripe(store, "infra", tags=("fix:infra",))
    _gripe(store, "untagged", tags=())
    _gripe(store, "unknown", tags=("fix:other",))
    assert gd.candidates(store, 10) == []


def test_mixed_classes_share_the_one_code_capacity(
    store: Store, two_lanes: Any
) -> None:
    """Capacity 3 (codex 1 + claude-sub 2): a fix:docs chain holds a slot
    against fix:code ones — one pool, not a per-class split."""
    _light(store)
    for i, tag in enumerate(("fix:docs", "fix:skill", "fix:web", "fix:code")):
        _gripe(store, f"g{i}", tags=(tag,), prio=i + 1)
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert res.extra["lane_capacity"] == 3 and res.extra["slots"] == 3
    assert (res.claimed, res.ok) == (3, 3)
    assert gd.open_chain_count(store) == 0
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert res.extra["slots"] == 0 and res.claimed == 0


def test_pass_mints_within_free_slots_and_is_idempotent(
    store: Store, two_lanes: Any
) -> None:
    _light(store)
    a = _gripe(store, "alpha", prio=1)
    b = _gripe(store, "beta", prio=2)
    c = _gripe(store, "gamma", prio=3)
    assert gd.open_chain_count(store) == 0
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    # capacity 3, no held slots: one fire fills every free build slot
    assert (res.claimed, res.ok, res.failed) == (3, 3, 0)
    assert res.extra["slots"] == 3 and res.extra["lane_capacity"] == 3
    assert len(_roots_for(store, a)) == 1 and len(_roots_for(store, b)) == 1
    assert len(_roots_for(store, c)) == 1
    [note] = _comments(store, a)
    assert note.startswith(
        f"dispatch: minted gripe-fix-direct td{_roots_for(store, a)[0]}"
    )
    assert gd.open_chain_count(store) == 0

    # Second fire: all three are linked (skipped) and no slot is free.
    _gripe(store, "delta", prio=1)
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert (res.claimed, res.ok) == (0, 0) and res.extra["slots"] == 0
    assert len(_roots_for(store, a)) == 1  # never doubled


@pytest.mark.parametrize("namespace", ["open", "flag"])
def test_unsupported_project_is_skipped_with_one_comment(
    store: Store, two_lanes: Any, namespace: str
) -> None:
    """gr611473: the precis factory cannot land a catpath change."""
    _light(store)
    gid = _gripe(store, "catpath code bug")
    project = (
        Tag.open("project:catpath")
        if namespace == "open"
        else Tag.flag("project:catpath")
    )
    store.add_tag(gid, project, set_by="agent")
    for _ in range(2):
        res = gd.run_gripe_dispatch_pass(store, host="h1")
        assert (res.ok, res.failed) == (0, 0)
        assert _roots_for(store, gid) == []
        [note] = _comments(store, gid)
        assert "dispatch:" in note and "skip" in note.lower()
        assert "project" in note.lower() and "precis" in note.lower()
    assert gd.open_chain_count(store) == 0


@pytest.mark.parametrize(
    "project_tags",
    [(), ("project:precis",), ("project:precis-mcp",), ("project:dark-factory",)],
)
def test_supported_or_default_project_dispatches(
    store: Store, two_lanes: Any, project_tags: tuple[str, ...]
) -> None:
    _light(store)
    gid = _gripe(store, "precis code bug", tags=("fix:code", *project_tags))
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert (res.claimed, res.ok, res.failed) == (1, 1, 0)
    assert len(_roots_for(store, gid)) == 1


@pytest.mark.parametrize(
    "project_tags",
    [
        ("project:platform",),
        ("project:memory",),
        ("project:taproot",),
        ("project:quests", "project:precis"),
    ],
)
def test_in_repo_area_project_dispatches(
    store: Store, two_lanes: Any, project_tags: tuple[str, ...]
) -> None:
    """gr752647: triage labels areas of this repository with ``project:``."""
    _light(store)
    gid = _gripe(store, "platform code bug", tags=("fix:code", *project_tags))
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert (res.claimed, res.ok, res.failed) == (1, 1, 0)
    assert len(_roots_for(store, gid)) == 1
    assert not any(c.startswith(gd._PROJECT_SKIP_PREFIX) for c in _comments(store, gid))


_ELSEWHERE = (
    "catpath",
    "autocatpath",
    "infra",
    "cluster",
    "dft",
    "local-compute",
    "ops",
    "deploy",
)


@pytest.mark.parametrize("project", _ELSEWHERE)
def test_every_elsewhere_project_is_skipped(
    store: Store, two_lanes: Any, project: str
) -> None:
    _light(store)
    gid = _gripe(store, "elsewhere", tags=("fix:code", f"project:{project}"))
    assert gd.run_gripe_dispatch_pass(store, host="h1").ok == 0
    assert _roots_for(store, gid) == []
    [note] = _comments(store, gid)
    assert note.startswith(gd._PROJECT_SKIP_PREFIX)


def test_elsewhere_set_is_the_decided_one() -> None:
    assert frozenset(_ELSEWHERE) == gd._ELSEWHERE_PROJECTS


def test_earlier_project_skip_comment_does_not_block_a_later_pick(
    store: Store, two_lanes: Any
) -> None:
    """A gripe skipped under the old allow-list carries the skip comment;
    once its project is no longer elsewhere the pass builds it anyway."""
    _light(store)
    gid = _gripe(store, "skipped before", tags=("fix:code", "project:platform"))
    gd._append_comment(
        store, gid, f"{gd._PROJECT_SKIP_PREFIX} factory can only land project:precis."
    )
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert (res.claimed, res.ok, res.failed) == (1, 1, 0)
    assert len(_roots_for(store, gid)) == 1


def test_supported_project_does_not_override_an_unsupported_project(
    store: Store, two_lanes: Any
) -> None:
    _light(store)
    gid = _gripe(
        store,
        "cross-project bug",
        tags=("fix:code", "project:precis", "project:catpath"),
    )
    assert gd.run_gripe_dispatch_pass(store, host="h1").ok == 0
    assert _roots_for(store, gid) == []
    assert len(_comments(store, gid)) == 1


def test_unsupported_projects_do_not_use_free_build_slots(
    store: Store, two_lanes: Any
) -> None:
    _light(store)
    blocked = [
        _gripe(store, f"catpath {i}", tags=("fix:code", "project:catpath"), prio=1)
        for i in range(4)
    ]
    supported = [_gripe(store, f"precis {i}", prio=5) for i in range(3)]
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert (res.ok, res.failed) == (3, 0)
    assert all(_roots_for(store, gid) == [] for gid in blocked)
    assert all(len(_roots_for(store, gid)) == 1 for gid in supported)


@pytest.mark.parametrize("fix_age, expected", [(5, 0), (20, 1)])
def test_dispatch_age_starts_when_fix_code_is_attached(
    store: Store, two_lanes: Any, fix_age: int, expected: int
) -> None:
    """gr611300: a long-filed gripe still gets a triage grace period."""
    _light(store)
    gid = _gripe(store, "old report newly marked code", age_minutes=1440)
    _tag_age(store, gid, "fix:code", fix_age)
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert (res.ok, res.failed) == (expected, 0)
    assert len(_roots_for(store, gid)) == expected


@pytest.mark.parametrize(
    "triaged_age, fix_age, expected",
    [(5, 20, 0), (20, 5, 0), (20, 30, 1), (30, 20, 1)],
)
def test_dispatch_age_uses_latest_triage_or_fix_code_attachment(
    store: Store,
    two_lanes: Any,
    triaged_age: int,
    fix_age: int,
    expected: int,
) -> None:
    _light(store)
    gid = _gripe(store, "triaged old report", status="triaged", age_minutes=1440)
    _tag_age(store, gid, "triaged", triaged_age)
    _tag_age(store, gid, "fix:code", fix_age)
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert (res.ok, res.failed) == (expected, 0)
    assert len(_roots_for(store, gid)) == expected


def test_recent_open_status_does_not_reset_dispatch_age(
    store: Store, two_lanes: Any
) -> None:
    _light(store)
    gid = _gripe(store, "code gripe with recent open status", age_minutes=1440)
    _tag_age(store, gid, "fix:code", 20)
    _tag_age(store, gid, "open", 5)
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert (res.ok, res.failed) == (1, 0)
    assert len(_roots_for(store, gid)) == 1


def test_one_tick_fills_every_free_build_slot(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr584410: the per-tick cap is the free-slot count — devin 6 +
    claude-sub 3 with no live builds mints nine chains in one pass, and
    never more than the free slots."""
    use_lanes(
        monkeypatch,
        Lane(
            "devin",
            "devin_tree",
            "free-promo",
            6,
            frozenset({"code"}),
            vendor="cognition",
        ),
        Lane(
            "claude-sub",
            "claude_p",
            "sub",
            3,
            frozenset({ANY_CLASS}),
            vendor="anthropic",
            models=("claude-sonnet-5-5",),
            model_prefix="claude-",
        ),
    )
    _light(store)
    gripes = [_gripe(store, f"g{i}", prio=1) for i in range(12)]
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert (res.claimed, res.ok, res.failed) == (9, 9, 0)
    assert res.extra["slots"] == 9 and res.extra["lane_capacity"] == 9
    # equal prios → oldest first
    assert [m["gripe"] for m in res.extra["minted"]] == gripes[:9]
    assert gd.open_chain_count(store) == 0
    # no free slot left: the next fire mints nothing more
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert (res.claimed, res.ok) == (0, 0) and res.extra["slots"] == 0


def _build_node(store: Store, root_id: int) -> int:
    """The unclassed ``op`` child of a plan root — the chain's build."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE parent_id = %s "
            "AND meta ? 'op' AND meta->>'task_class' IS NULL",
            (root_id,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_landed_build_frees_the_slot_while_the_chain_runs_on(
    store: Store, two_lanes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr584410: a chain parked on the sha-in-prod gate, validate or the
    non-blocking review holds no build slot — the slot frees at land."""
    codex, _sub = two_lanes
    use_lanes(monkeypatch, codex)  # capacity 1
    _light(store)
    a = _gripe(store, "a", prio=1)
    b = _gripe(store, "b", prio=2)
    assert gd.run_gripe_dispatch_pass(store, host="h1").ok == 1
    [root] = _roots_for(store, a)
    _queue_build(store, _build_node(store, root))
    assert gd.run_gripe_dispatch_pass(store, host="h1").ok == 0  # full

    # the build lands: its node goes done while gate/validate/review
    # stay open — the chain runs on for hours yet holds no slot
    build = _build_node(store, root)
    store.add_tag(
        build, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
    )
    assert gd.open_chain_count(store) == 0
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert res.ok == 1 and _roots_for(store, b)
    # a still has its (running-on) chain linked: not re-minted
    assert _roots_for(store, a) == [root]


def test_factory_off_mints_nothing(
    store: Store, two_lanes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _light(store)
    _gripe(store, "a")
    monkeypatch.setenv("PRECIS_FACTORY", "off")
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert (res.claimed, res.ok, res.failed) == (0, 0, 0)
    assert res.extra["read_only"] == "PRECIS_FACTORY=off"
    with store.pool.connection() as conn:
        n_links = conn.execute(
            "SELECT count(*) FROM links WHERE relation = 'related-to'"
        ).fetchone()
    assert n_links is not None and int(n_links[0]) == 0


@pytest.mark.parametrize("tag", gd.HOLD_TAGS)
def test_every_hold_tag_excludes(store: Store, two_lanes: Any, tag: str) -> None:
    _light(store)
    _gripe(store, "held", tags=("fix:code", tag))
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert (res.claimed, res.ok) == (0, 0)


def test_closed_chain_frees_its_slot_and_its_gripe_reenters(
    store: Store, two_lanes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr609044: a chain that closed (done / won't-do) is dead work —
    its gripe re-enters the lane instead of staying linked forever."""
    codex, _sub = two_lanes
    use_lanes(monkeypatch, codex)  # capacity 1
    _light(store)
    a = _gripe(store, "a", prio=1)
    b = _gripe(store, "b", prio=5)
    assert gd.run_gripe_dispatch_pass(store, host="h1").ok == 1
    [root] = _roots_for(store, a)
    _queue_build(store, _build_node(store, root))
    assert gd.run_gripe_dispatch_pass(store, host="h1").ok == 0  # full
    for node in (root, _build_node(store, root)):
        store.add_tag(
            node, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
        )
    assert gd.open_chain_count(store) == 0
    assert a in {c.gripe_id for c in gd.candidates(store, 10)}
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    # a's closed chain no longer blocks it; a outscores b (prio 1 over
    # 5), so the one free slot re-mints a second chain for a
    assert res.ok == 1 and len(_roots_for(store, a)) == 2
    assert _roots_for(store, b) == []


def test_linked_todo_terminal_status_frees_the_gripe(
    store: Store, two_lanes: Any
) -> None:
    """gr609044: a ``related-to`` todo whose STATUS is terminal (done,
    won't-do, auto-timeout) no longer holds its gripe out; one that is
    open/doing/blocked — or carries no STATUS — still does."""
    from precis.todo import TERMINAL_TODO_STATUSES

    cases = [
        ("done", "done"),
        ("wontdo", "won't-do"),
        ("timeout", "auto-timeout"),
        ("open", "open"),
        ("doing", "doing"),
        ("blocked", "blocked"),
        ("nostatus", None),
    ]
    freed: list[int] = []
    held: list[int] = []
    for title, status in cases:
        gid = _gripe(store, f"linked {title}", prio=5)
        todo = store.insert_ref(kind="todo", slug=None, title=f"work for {gid}")
        if status is not None:
            store.add_tag(todo.id, Tag.closed("STATUS", status), set_by="agent")
        store.add_link(src_ref_id=todo.id, dst_ref_id=gid, relation="related-to")
        (freed if status in TERMINAL_TODO_STATUSES else held).append(gid)
    unlinked = _gripe(store, "unlinked", prio=5)
    picks = {c.gripe_id for c in gd.candidates(store, 10)}
    for gid in freed + [unlinked]:
        assert gid in picks
    for gid in held:
        assert gid not in picks


def test_hand_minted_chain_counts(store: Store, two_lanes: Any) -> None:
    from precis.plans.templates import mint_gripe_fix_direct

    _light(store)
    a = _gripe(store, "a")
    mint_gripe_fix_direct(store, gripe_id=a)
    assert gd.open_chain_count(store) == 0
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert res.claimed == 0 and len(_roots_for(store, a)) == 1


def test_queue_depth_mints_a_bounded_buffer_from_host_capacity(
    store: Store,
    two_lanes: Any,
) -> None:
    """gr702610: N free host slots leave at least N queued builds in one fire.

    Lane supply is three, but this host has two build executor slots.
    Default depth two freezes only four of the ten eligible gripes.
    """
    _light(store)
    psettings.set_setting("factory.gripe_dispatch.queue_depth", 2, store=store)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE app_state SET value = '3' "
            "WHERE key = 'claim.executor_cap.ssh_node.h1'"
        )
    gripes = [_gripe(store, f"buffered {i}") for i in range(10)]
    assert gd.build_queue_supply(store) == (2, 0)
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert res.ok == 4
    assert gd.build_queue_supply(store) == (2, 4)
    assert gd.open_chain_count(store) == 0
    assert len(gd.candidates(store, 20)) == 6
    assert not _roots_for(store, gripes[-1])
    assert gd.run_gripe_dispatch_pass(store, host="h1").ok == 0


def test_queue_supply_ignores_dark_and_disabled_hosts(
    store: Store, two_lanes: Any
) -> None:
    _light(store)
    assert gd.build_queue_supply(store)[0] == 3
    with store.pool.connection() as conn:
        conn.execute("UPDATE host_heartbeat SET ts = now() - interval '1 day'")
    assert gd.build_queue_supply(store)[0] == 0
    with store.pool.connection() as conn:
        conn.execute("UPDATE host_heartbeat SET ts = now()")
        conn.execute(
            "INSERT INTO service_config (host, service, prio) "
            "VALUES ('h1', 'job_ssh_node', 0) "
            "ON CONFLICT (host, service) DO UPDATE SET prio = 0"
        )
    assert gd.build_queue_supply(store)[0] == 0


def test_deny_path_report_routes_to_the_coordinator(
    store: Store, two_lanes: Any
) -> None:
    """gr645163: a gripe whose report names a factory deny path mints no
    chain — work no builder lane may write becomes one ask-coordinator
    ask carrying the report, and ``hold:no-dispatch`` keeps the gripe
    out of later picks until a human re-judges it."""
    _light(store)
    gid = _gripe(store, "Fix: add a --remote flag to scripts/ship")
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert (res.claimed, res.ok, res.failed) == (1, 1, 0)
    assert res.extra["minted"] == []
    (routed,) = res.extra["routed"]
    assert routed["gripe"] == gid
    ask = int(routed["ask"])
    ask_tags = [str(t.value) for t in store.tags_for(ask) if t.namespace == "open"]
    assert "ask-coordinator" in ask_tags
    gripe_tags = [str(t.value) for t in store.tags_for(gid) if t.namespace == "open"]
    assert "hold:no-dispatch" in gripe_tags
    assert _roots_for(store, gid) == [ask]
    assert any(
        c.startswith("dispatch: routes to the coordinator")
        for c in _comments(store, gid)
    )
    # The ask's related-to link holds the gripe while it is open; the
    # hold tag holds it after — a second pass picks nothing.
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert res.claimed == 0


def test_hidden_comment_does_not_route(store: Store, two_lanes: Any) -> None:
    """gr653037: hiding a stale comment that names a deny path releases
    the gripe — neither the route check nor the build's embedded report
    reads it (gr573439 was held again after its comment was hidden)."""
    from precis.plans import route
    from precis.plans.templates import _gripe_report

    _light(store)
    gid = _gripe(store, "fix the widget flake in src/precis/todo/guards.py")
    gd._append_comment(store, gid, "the fix is in scripts/ship")
    hidden = max(int(c.ord) for c in store.chunks.list_chunks_for_ref(gid))
    store.stamp_ref_meta(gid, {"hidden_comments": [hidden]})
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert "routed" not in res.extra
    assert [m["gripe"] for m in res.extra["minted"]] == [gid]
    report = _gripe_report(store, gid, f"gr{gid}", "t")
    assert "scripts/ship" not in report
    assert route.coordinator_route(report, ("scripts/ship",)) is None


def test_routing_comment_does_not_reroute(store: Store, two_lanes: Any) -> None:
    """gr653037: the dispatcher's own routing comment quotes the deny path;
    a released gripe (hold tag removed) must not route on it again."""
    _light(store)
    gid = _gripe(store, "fix the widget flake in src/precis/todo/guards.py")
    gd._append_comment(
        store,
        gid,
        "dispatch: routes to the coordinator — report names deny path "
        "`scripts/ship` (matched `scripts/ship`); ask td1. No chain minted; "
        "`hold:no-dispatch` holds the gripe — remove the tag to release it "
        "back to dispatch.",
    )
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert "routed" not in res.extra
    assert [m["gripe"] for m in res.extra["minted"]] == [gid]


def test_route_builder_tag_skips_the_deny_path_route(
    store: Store, two_lanes: Any
) -> None:
    """gr685970: ``route:builder`` is the coordinator's read-the-report
    override — the gripe mints its chain although its report asks to
    write a deny path (the mint/write gates still guard real writes)."""
    _light(store)
    gid = _gripe(
        store,
        "Fix: add a --remote flag to scripts/ship",
        tags=("fix:code", gd.ROUTE_BUILDER_TAG),
    )
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert "routed" not in res.extra
    assert [m["gripe"] for m in res.extra["minted"]] == [gid]


def test_route_builder_tag_keeps_the_graph_only_route(
    store: Store, two_lanes: Any
) -> None:
    """gr685970: the override skips the deny-path leg only — a
    prod-graph-only report still has no repo diff for a builder."""
    _light(store)
    gid = _gripe(
        store,
        "gr630346 me123456 td999",
        tags=("fix:code", gd.ROUTE_BUILDER_TAG),
    )
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    (routed,) = res.extra["routed"]
    assert routed["gripe"] == gid


def test_graph_only_report_routes_to_the_coordinator(
    store: Store, two_lanes: Any
) -> None:
    """gr645163: a gripe reduced to graph handles is prod-graph work
    (gr630346/gr630611's class of no-commit runs) — no chain, one
    coordinator ask."""
    _light(store)
    gid = _gripe(store, "gr630346 me123456 td999")
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert (res.claimed, res.ok, res.failed) == (1, 1, 0)
    assert res.extra["minted"] == []
    (routed,) = res.extra["routed"]
    assert routed["gripe"] == gid


def test_enabled_resolves_per_host(store: Store) -> None:
    _light(store, prio=1, host="*")
    _light(store, prio=0, host="dim")
    assert gd.gripe_dispatch_enabled(store, host="lit")
    assert not gd.gripe_dispatch_enabled(store, host="dim")


def test_hold_tags_keep_rescue_intake_not_dark_factory() -> None:
    # gr591017: ``rescue:intake`` (a gripe that failed three mints) stays
    # held; ``cluster:dark-factory`` is no hold — it is also the cluster
    # label humans, agents and gripe_triage put on engine design gripes,
    # and the rescue-filed gripes it was meant to stop never carry
    # fix:code, so candidates() already excludes them.
    assert "rescue:intake" in gd.HOLD_TAGS
    assert "cluster:dark-factory" not in gd.HOLD_TAGS


def test_fix_code_gripe_tagged_dark_factory_is_a_candidate(
    store: Store, two_lanes: Any
) -> None:
    """gr591017: a triaged fix:code engine gripe — human- or agent-filed,
    cluster-labelled ``cluster:dark-factory`` — dispatches again."""
    _light(store)
    gid = _gripe(
        store, "engine design gripe", tags=("fix:code", "cluster:dark-factory")
    )
    assert gid in [c.gripe_id for c in gd.candidates(store, 10)]
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert res.ok == 1 and len(_roots_for(store, gid)) == 1


def test_rescue_filed_engine_gripe_still_not_a_candidate(
    store: Store, two_lanes: Any
) -> None:
    """gr591017: the dropped hold guarded nothing — a factory_rescue
    engine gripe carries ``cluster:dark-factory`` but never ``fix:code``
    (and no agent/human ``filed_by``), so it stays out of candidates."""
    from precis.workers import factory_rescue as fr

    node = int(store.insert_ref(kind="todo", slug=None, title="stalled slice").id)
    res = fr.enter_rescue(
        store,
        node,
        "stalled",
        fr.RescueEvidence(step="stall pass", error_line="boom"),
    )
    assert res.gripe_id is not None
    open_tags = [
        str(t.value) for t in store.tags_for(res.gripe_id) if t.namespace == "open"
    ]
    assert "cluster:dark-factory" in open_tags and "fix:code" not in open_tags
    _age(store, res.gripe_id)
    assert res.gripe_id not in {c.gripe_id for c in gd.candidates(store, 10)}


def test_read_only_reason_mints_nothing(store: Store, two_lanes: Any) -> None:
    _light(store)
    _gripe(store, "a")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO service_config (host, service, prio) "
            "VALUES ('*', 'plan_dispatch', 0)"
        )
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert (res.claimed, res.ok, res.failed) == (0, 0, 0)
    assert res.extra["read_only"]
    with store.pool.connection() as conn:
        n_links = conn.execute(
            "SELECT count(*) FROM links WHERE relation = 'related-to'"
        ).fetchone()
    assert n_links is not None and int(n_links[0]) == 0


def test_three_failed_mints_rescue_the_gripe(
    store: Store, two_lanes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _light(store)
    g = _gripe(store, "always fails")
    calls: list[int] = []

    def boom(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        raise RuntimeError("mint blew up")

    monkeypatch.setattr(gd, "mint_gripe_fix_direct", boom)
    for _ in range(3):
        res = gd.run_gripe_dispatch_pass(store, host="h1")
        assert (res.claimed, res.ok, res.failed) == (1, 0, 1)
    assert len(calls) == 3

    open_tags = [str(t.value) for t in store.tags_for(g) if t.namespace == "open"]
    assert "rescue:intake" in open_tags
    fails = [c for c in _comments(store, g) if c.startswith("dispatch: mint failed:")]
    assert len(fails) == 3
    assert any(c.startswith("rescue: intake") for c in _comments(store, g))

    # rescue:intake is a hold tag: the gripe leaves candidates, so a
    # fourth pass never reaches the mint
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert res.claimed == 0 and len(calls) == 3


def test_cadence_and_service_spec() -> None:
    from precis.workers.registry import SERVICES_BY_NAME, ServiceKind
    from precis.workers.scheduler import CADENCES

    cad = next(c for c in CADENCES if c.name == "gripe_dispatch")
    assert cad.interval_s == 10 * 60
    # service-gated so a row-less host leaves the fire to a lit one, with
    # claim_when_dark so a fleet-wide dark pass still wins its lease, else
    # the cadence-staleness check reads it as "stopped firing" (al567129).
    assert cad.service == "gripe_dispatch" and cad.claim_when_dark is True
    assert cad.spends is False
    spec = SERVICES_BY_NAME["gripe_dispatch"]
    assert spec.kind is ServiceKind.PASS
    assert not spec.default_profiles and spec.ref_pass is False
    assert spec.uses_model is False


# ── the ci_watch door: meta.ci.sig + filed_by.principal = worker:ci_watch ──
#
# The door key is the write principal, never the agent-supplied client
# or actor: a stdio ``precis serve`` client binds no principal and falls
# through to ``worker:<PRECIS_CURRENT_SOURCE|cli>``, so only the
# ``job_inproc`` tick's own bound source lands ``worker:ci_watch`` —
# ``ci_watch.WORKER_PRINCIPAL``, the one constant both sides name.


def _age(store: Store, ref_id: int, minutes: float = 120.0) -> int:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - make_interval(secs => %s) "
            "WHERE ref_id = %s",
            (minutes * 60, ref_id),
        )
    _age_dispatch_tags(store, ref_id, minutes)
    return ref_id


def test_inproc_ci_watch_gripe_is_a_candidate(store: Store, two_lanes: Any) -> None:
    """A gripe written under the inproc tick context ``ci_watch`` with
    ``meta.ci.sig`` stamps ``filed_by.principal = worker:ci_watch`` and
    is a candidate."""
    from precis.utils.inproc_context import TickContext, tick_context
    from precis.workers.job_types import ci_watch

    with tick_context(TickContext(source=ci_watch.NAME)):
        ref = insert_gripe(
            store,
            "nightly red: ci:check.yml:tests/test_x.py::test_y",
            meta={"ci": {"sig": "ci:check.yml:tests/test_x.py::test_y"}},
        )
    meta = ref.meta or {}
    assert (meta.get("filed_by") or {}).get("principal") == (ci_watch.WORKER_PRINCIPAL)
    gid = int(ref.id)
    store.add_tag(gid, Tag.open("fix:code"), set_by="agent")
    _age(store, gid)
    assert gid in [c.gripe_id for c in gd.candidates(store, 10)]


def test_real_run_pass_stamps_the_door_principal(
    store: Store, two_lanes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: a queued ``ci_watch`` job claimed by
    ``run_job_inproc_pass`` files a gripe whose stored
    ``meta.filed_by.principal`` equals ``ci_watch.WORKER_PRINCIPAL`` —
    the executor's tick binding is what the dispatch door keys on — and
    the dispatch SQL admits it."""
    from precis.workers.executors import job_inproc
    from precis.workers.job_types import ci_watch

    def gh_json(endpoint: str) -> Any:
        endpoints = {
            "repos/o/r/actions/workflows/check.yml/runs?event=schedule&per_page=3": {
                "workflow_runs": [
                    {
                        "id": 201,
                        "status": "completed",
                        "conclusion": "failure",
                        "event": "schedule",
                        "head_branch": "main",
                        "head_sha": "d" * 40,
                        "html_url": "https://github.com/o/r/actions/runs/201",
                        "updated_at": "2026-11-07T03:10:00Z",
                    }
                ]
            },
            "repos/o/r/actions/workflows/check.yml/runs?per_page=20": {
                "workflow_runs": []
            },
            "repos/o/r/actions/runs/201/jobs": {
                "jobs": [
                    {
                        "id": 9001,
                        "name": "pytest",
                        "status": "completed",
                        "conclusion": "failure",
                    }
                ]
            },
        }
        if endpoint not in endpoints:
            raise AssertionError(f"unstubbed gh endpoint: {endpoint}")
        return endpoints[endpoint]

    monkeypatch.setattr(ci_watch, "_gh_json", gh_json)
    monkeypatch.setattr(
        ci_watch,
        "_gh_log",
        lambda repo, job_id: (
            "2026-11-07T03:00:01.0000000Z FAILED "
            "tests/test_x.py::test_y - assert 1 == 2\n"
        ),
    )
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="ci_watch tick",
        meta={
            "executor": "job_inproc",
            "job_type": ci_watch.NAME,
            "params": {"repo": "o/r"},
        },
    )
    store.add_tag(job.id, Tag.closed("STATUS", "queued"), set_by="agent")
    assert job_inproc.run_job_inproc_pass(store, limit=1)["ok"] == 1

    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta FROM refs WHERE kind = 'gripe'"
        ).fetchall()
    assert len(rows) == 1
    gid, meta = int(rows[0][0]), dict(rows[0][1] or {})
    assert (meta.get("ci") or {}).get("sig") == ("ci:check.yml:tests/test_x.py::test_y")
    assert (meta.get("filed_by") or {}).get("principal") == (ci_watch.WORKER_PRINCIPAL)
    _age(store, gid)
    assert gid in [c.gripe_id for c in gd.candidates(store, 10)]


def test_mcp_put_handler_cannot_forge_the_ci_door(
    store: Store, hub: Any, two_lanes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gripe put HANDLER swallows ``meta=`` (``accepts_put_meta`` is
    False for kind=gripe): a caller-supplied ``meta.ci`` /
    ``meta.filed_by`` never reaches the ref — no ``ci.sig``, and
    ``filed_by.principal`` is the server stamp. This test is the red a
    future handler must hit if it starts accepting ``put_meta`` — the
    ci_watch door key would then be re-ruled against that surface, not
    silently widened."""
    _trust(store, "filer")  # the filer-door rule these tests pin
    from precis.handlers.gripe import GripeHandler
    from precis.workers.job_types import ci_watch

    monkeypatch.delenv("PRECIS_CURRENT_SOURCE", raising=False)
    GripeHandler(hub=hub).put(
        text="forged ci gripe — never a candidate",
        meta={
            "filed_by": {"principal": ci_watch.WORKER_PRINCIPAL},
            "ci": {"sig": "ci:check.yml:tests/x.py::t"},
        },
    )
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id, meta FROM refs WHERE kind = 'gripe' "
            "ORDER BY ref_id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    gid, meta = int(row[0]), dict(row[1] or {})
    assert meta.get("ci") is None
    assert (meta.get("filed_by") or {}).get("principal") != (ci_watch.WORKER_PRINCIPAL)
    store.add_tag(gid, Tag.open("fix:code"), set_by="agent")
    _age(store, gid)
    assert gid not in [c.gripe_id for c in gd.candidates(store, 10)]


def test_worker_cli_with_ci_sig_is_not_a_candidate(
    store: Store, two_lanes: Any
) -> None:
    """A ``worker:cli``-stamped gripe carrying ``meta.ci.sig`` is NOT a
    candidate — the old ``actor = 'agent'`` clause is gone: the
    agent-declared actor no longer opens the door."""
    _trust(store, "filer")  # the filer-door rule these tests pin
    from precis.workers.job_types import ci_watch

    gid = _gripe(
        store,
        "nightly red: ci:check.yml:tests/test_x.py::test_y",
        meta={
            "ci": {"sig": "ci:check.yml:tests/test_x.py::test_y"},
            "filed_by": {"actor": "agent", "principal": "worker:cli"},
        },
    )
    ref = store.get_ref(kind="gripe", id=gid)
    assert ref is not None
    meta = ref.meta or {}
    assert meta["ci"]["sig"] == "ci:check.yml:tests/test_x.py::test_y"
    assert meta["filed_by"]["principal"] != ci_watch.WORKER_PRINCIPAL
    assert gid not in [c.gripe_id for c in gd.candidates(store, 10)]


def test_worker_gripe_without_ci_sig_is_not_a_candidate(
    store: Store, two_lanes: Any
) -> None:
    _trust(store, "filer")  # the filer-door rule these tests pin
    gid = _gripe(store, "worker-filed gripe", meta=_WORKER)
    assert gid not in [c.gripe_id for c in gd.candidates(store, 10)]


# ── release-waiver gripes: a broken build (Reto 2026-11-10) ────────────


def _waiver_gripe(
    store: Store,
    title: str = "release r41 shipped with known-red tests: tests/t.py::a",
    *,
    rn: str = "r41",
    age_minutes: float = 0.1,
    with_meta: bool = True,
    tags: tuple[str, ...] = ("fix:code", "broken-build"),
) -> int:
    meta: dict[str, Any] = {"release_waiver": {"rN": rn, "tests": ["tests/t.py::a"]}}
    return _gripe(
        store,
        title,
        meta=meta if with_meta else {},
        tags=(*tags, f"release-waiver:{rn}"),
        age_minutes=age_minutes,
        prio=1,
    )


def test_waiver_gripe_skips_the_settle_delay_and_the_trust_check(
    store: Store, two_lanes: Any
) -> None:
    """Seconds old, no ``filed_by`` stamp, ``filer`` trust: still a
    candidate, ahead of a hotter and older ordinary gripe."""
    _trust(store, "filer")
    ordinary = _gripe(store, "ordinary", prio=1, age_minutes=600)
    young = _gripe(store, "young ordinary", age_minutes=0.1)
    waiver = _waiver_gripe(store)
    picks = gd.candidates(store, 10)
    assert [c.gripe_id for c in picks] == [waiver, ordinary]
    assert picks[0].waiver and not picks[1].waiver
    assert young not in [c.gripe_id for c in picks]


def test_waiver_tag_alone_or_meta_alone_is_not_the_door(
    store: Store, two_lanes: Any
) -> None:
    """Both the tag and the writer's ``meta.release_waiver`` are needed —
    a reader's tag does not skip the settle delay."""
    _trust(store, "filer")
    _waiver_gripe(store, "tag only", rn="r1", with_meta=False)
    _gripe(
        store,
        "meta only",
        meta={"release_waiver": {"rN": "r2"}},
        age_minutes=0.1,
    )
    assert gd.candidates(store, 10) == []


@pytest.mark.parametrize("junk", [None, "yes", 1, [], True])
def test_waiver_meta_must_be_a_json_object(
    store: Store, two_lanes: Any, junk: Any
) -> None:
    """``meta.release_waiver`` of JSON null (or any non-object) is not the
    writer's record: the gripe keeps the settle delay and trust check."""
    _trust(store, "filer")
    _gripe(
        store,
        "null waiver meta",
        meta={"release_waiver": junk},
        tags=("fix:code", "release-waiver:r9"),
        age_minutes=0.1,
    )
    assert gd.candidates(store, 10) == []


def test_waiver_gripe_keeps_the_hold_tags(store: Store, two_lanes: Any) -> None:
    _waiver_gripe(store, tags=("fix:code", "hold:no-dispatch"))
    assert gd.candidates(store, 10) == []


def test_pass_mints_a_waiver_chain_at_once_beyond_the_slots(
    store: Store, two_lanes: Any
) -> None:
    """Capacity 3 is full of ordinary chains; the waiver gripe still
    mints, its chain root carries the release claim band, and the
    ordinary chains do not."""
    _light(store)
    ordinary = [_gripe(store, f"g{i}", prio=i + 1) for i in range(3)]
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert (res.ok, res.extra["slots"]) == (3, 3)
    waiver = _waiver_gripe(store)
    more = _gripe(store, "one more ordinary", prio=1)
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert res.extra["waivers"] == [waiver]
    assert res.extra["slots"] == 0  # the waiver chain took what was left
    assert res.ok == 1 and res.failed == 0
    [root] = _roots_for(store, waiver)
    assert _roots_for(store, more) == []  # no slot for the ordinary one
    assert _todo_meta(store, root)["claim_band"] == "release"
    for gid in ordinary:
        [r] = _roots_for(store, gid)
        assert "claim_band" not in _todo_meta(store, r)
    assert any(
        c.startswith("dispatch: minted gripe-fix-direct")
        for c in _comments(store, waiver)
    )


def _todo_meta(store: Store, ref_id: int) -> dict[str, Any]:
    ref = store.get_ref(kind="todo", id=ref_id)
    assert ref is not None
    return dict(ref.meta or {})


def test_waiver_chain_jobs_carry_the_band_ordinary_ones_do_not(
    store: Store, two_lanes: Any
) -> None:
    """``plan_dispatch._mint_job`` copies the root's band onto the build's
    run_slice job (and only for the build/land job types)."""
    from datetime import UTC, datetime

    from precis.workers.job_types import plan_dispatch as pd

    waiver = _waiver_gripe(store)
    plain = _gripe(store, "plain")
    band_mint = gd.mint_gripe_fix_direct(store, gripe_id=waiver, claim_band="release")
    plain_mint = gd.mint_gripe_fix_direct(store, gripe_id=plain)

    def node(mint: Any) -> Any:
        return pd._Node(
            id=mint.ids["build"],
            kind="todo",
            parent_id=mint.root_id,
            title="build",
            meta={},
            status="open",
            created_at=datetime.now(UTC),
            prio=None,
            depth=1,
            root=mint.root_id,
        )

    def job_meta(mint: Any, job_type: str) -> dict[str, Any]:
        jid = pd._mint_job(
            store,
            node(mint),
            job_type,
            "ssh_node",
            {"todo_id": mint.ids["build"]},
            idem_key=f"test:{job_type}:{mint.root_id}",
        )
        ref = store.get_ref(kind="job", id=jid)
        assert ref is not None
        return dict(ref.meta or {})

    assert job_meta(band_mint, "run_slice")["claim_band"] == "release"
    assert job_meta(plain_mint, "run_slice").get("claim_band") is None
    # a non-build job of the banded chain does not take the band
    assert job_meta(band_mint, "ci_verdict_poll").get("claim_band") is None


# ── parked builds hold no slot; stalled chains are swept (gr687946) ──


def _one_lane(monkeypatch: pytest.MonkeyPatch, cap: int = 1) -> None:
    use_lanes(
        monkeypatch, Lane("codex", "codex_exec", "codex", cap, frozenset({"code"}))
    )


def _chain_for(store: Store, gripe_id: int) -> tuple[int, int]:
    """Mint the gripe's chain; return ``(root, build node)``."""
    from precis.plans.templates import mint_gripe_fix_direct

    mint = mint_gripe_fix_direct(store, gripe_id=gripe_id)
    return int(mint.root_id), _build_node(store, int(mint.root_id))


def _age_node(store: Store, node: int, hours: float) -> None:
    """Make everything in the build node's whole plan (every todo tagged
    ``plan:<root>``, their tags and their jobs) ``hours`` old."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT parent_id FROM refs WHERE ref_id = %s", (node,)
        ).fetchone()
        assert row is not None
        root = int(row[0])
        ids = [
            int(r[0])
            for r in conn.execute(
                "SELECT rt.ref_id FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
                "WHERE t.namespace = 'OPEN' AND t.value = %s",
                (f"plan:{root}",),
            ).fetchall()
        ]
        conn.execute(
            "UPDATE refs SET updated_at = now() - make_interval(hours => %s) "
            "WHERE ref_id = ANY(%s) OR parent_id = ANY(%s)",
            (hours, ids, ids),
        )
        conn.execute(
            "UPDATE ref_tags SET created_at = now() - make_interval(hours => %s) "
            "WHERE ref_id = ANY(%s)",
            (hours, ids),
        )


def _queue_build(store: Store, build: int) -> None:
    """A queued ``run_slice`` job for the build node (what the dispatch
    tick queues for a doable build), so the pass sees a full queue."""
    from precis.workers._mint import mint_background_job

    with store.tx() as conn:
        mint_background_job(
            store,
            conn=conn,
            job_type="run_slice",
            executor="ssh_node",
            title="run_slice",
            params={"todo_id": build},
            parent_id=build,
            status="queued",
        )


def _park(store: Store, node: int, how: str) -> None:
    if how == "paused":
        store.add_tag(
            node, Tag.closed("STATUS", "paused"), set_by="system", replace_prefix=True
        )
    else:
        store.add_tag(node, Tag.open(how), set_by="system")


def _ask_in_plan(store: Store, root: int) -> int:
    ask = store.insert_ref(kind="todo", slug=None, title="a question", parent_id=root)
    store.add_tag(ask.id, Tag.open(f"plan:{root}"), set_by="system")
    store.add_tag(ask.id, Tag.open("ask-user"), set_by="system")
    store.add_tag(ask.id, Tag.closed("STATUS", "open"), set_by="system")
    return int(ask.id)


def _status(store: Store, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE rt.ref_id = %s AND t.namespace = 'STATUS'",
            (ref_id,),
        ).fetchone()
    return str(row[0]) if row else None


def _open_tags(store: Store, ref_id: int) -> set[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE rt.ref_id = %s AND t.namespace = 'OPEN'",
            (ref_id,),
        ).fetchall()
    return {str(r[0]) for r in rows}


@pytest.fixture(autouse=True)
def _fresh_parked_cache() -> Any:
    keys = (gd.MAX_PARKED_KEY, gd.STALL_HOURS_KEY, "factory.gripe_dispatch.queue_depth")
    for k in keys:
        psettings.invalidate(k)
    yield
    for k in keys:
        psettings.invalidate(k)


@pytest.mark.parametrize(
    "how", ["paused", "ask-user", "waiting-for:coordinator", "child-failed:td1"]
)
def test_parked_build_frees_its_slot(
    store: Store, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    _one_lane(monkeypatch)
    _light(store)
    a = _gripe(store, "a", prio=1)
    b = _gripe(store, "b", prio=5)
    _root, build = _chain_for(store, a)
    assert gd.open_chain_count(store) == 0  # undispatched: no running slot
    _park(store, build, how)
    assert gd.open_chain_count(store) == 0 and gd.parked_chain_count(store) == 1
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert res.ok == 1 and _roots_for(store, b)
    assert (res.extra["open_chains"], res.extra["parked"]) == (0, 1)


def test_live_build_holds_its_slot(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a running job or explicit claim keeps a running slot."""
    from precis.workers._mint import mint_background_job

    _one_lane(monkeypatch)
    _light(store)
    a = _gripe(store, "a", prio=1)
    b = _gripe(store, "b", prio=5)
    _root, build = _chain_for(store, a)
    _queue_build(store, build)
    assert gd.run_gripe_dispatch_pass(store, host="h1").ok == 0  # queued: target full
    _park(store, build, "waiting-for:coordinator")
    assert gd.open_chain_count(store) == 0
    with store.tx() as conn:
        mint_background_job(
            store,
            conn=conn,
            job_type="run_slice",
            executor="ssh_node",
            title="run_slice",
            params={"todo_id": build},
            parent_id=build,
            status="running",
        )
    assert gd.open_chain_count(store) == 1
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert res.ok == 0 and not _roots_for(store, b) and res.extra["slots"] == 0

    # a claim also counts as live
    c = _gripe(store, "c", prio=5)
    _root2, build2 = _chain_for(store, c)
    _park(store, build2, "paused")
    assert gd.open_chain_count(store) == 1
    store.add_tag(build2, Tag.open("claimed-by:agent-x"), set_by="system")
    assert gd.open_chain_count(store) == 2


def test_parked_chains_never_block_minting_below_the_backstop(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old "parked >= 2 x capacity: mint nothing" brake is gone — parked
    work costs no seat (Reto 2026-11-16). Far more parked chains than twice
    the lane capacity still leave minting alone."""
    _one_lane(monkeypatch, cap=1)
    _light(store)
    for i in range(4):  # 4 parked chains > 2 x capacity (1)
        g = _gripe(store, f"parked{i}", tags=("fix:code", "hold:no-dispatch"))
        _root, build = _chain_for(store, g)
        _park(store, build, "paused")
    fresh = _gripe(store, "fresh", prio=5)
    assert gd.parked_chain_count(store) == 4
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert res.extra["parked"] == 4 and res.extra["max_parked"] == 500
    assert res.ok == 1 and _roots_for(store, fresh)


def test_max_parked_remains_a_last_resort_backstop(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _one_lane(monkeypatch, cap=3)
    _light(store)
    psettings.set_setting(gd.MAX_PARKED_KEY, "1", store=store)
    a = _gripe(store, "a", prio=1)
    b = _gripe(store, "b", prio=5)
    _root, build = _chain_for(store, a)
    _park(store, build, "paused")
    assert gd.max_parked(store) == 1
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert (res.claimed, res.ok) == (0, 0) and not _roots_for(store, b)
    assert res.extra["parked"] == 1 and res.extra["max_parked"] == 1
    # raise the backstop: minting resumes
    psettings.set_setting(gd.MAX_PARKED_KEY, "2", store=store)
    assert gd.run_gripe_dispatch_pass(store, host="h1").ok == 1
    assert _roots_for(store, b)


def test_max_parked_defaults_to_500(store: Store) -> None:
    assert gd.DEFAULT_MAX_PARKED == 500
    assert gd.max_parked(store) == 500


def _asks_on(store: Store, root: int) -> list[int]:
    """Open ask todos in the chain's plan."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id FROM refs r JOIN ref_tags pt ON pt.ref_id = r.ref_id "
            "JOIN tags p ON p.tag_id = pt.tag_id AND p.namespace = 'OPEN' "
            "AND p.value = %s WHERE r.kind = 'todo' AND jsonb_typeof(r.meta->'ask') "
            "= 'object' ORDER BY r.ref_id",
            (f"plan:{root}",),
        ).fetchall()
    return [int(r[0]) for r in rows]


def test_sweep_asks_about_a_stalled_chain_and_does_not_close_it(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _one_lane(monkeypatch, cap=2)
    _light(store)
    a = _gripe(store, "a", prio=1)
    root, build = _chain_for(store, a)
    _park(store, build, "waiting-for:coordinator")
    store.stamp_ref_meta(
        build,
        {
            "parked": {
                "host": "build-host-1",
                "worktree": "/srv/wt/slice-td7",
                "head": "ab" * 20,
                "reason": "ladder spent",
                "at": "2026-11-16T10:00:00Z",
                "job": 1,
            }
        },
    )
    _age_node(store, build, 7)
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    # the root is NOT closed; the gripe does not re-enter dispatch
    assert _status(store, root) != "won't-do"
    assert len(_roots_for(store, a)) == 1 and res.ok == 0
    [z] = res.extra["stalled"]
    assert z["root"] == root and z["gripes"] == [a] and z["stalls"] == 1
    assert z["idle_hours"] >= 7
    [ask] = _asks_on(store, root)
    assert z["ask"] == ask
    ref = store.get_ref(kind="todo", id=ask)
    assert ref is not None
    assert ref.title == f"td{root}: chain stalled 7 h — resume, start over, or drop?"
    assert ref.meta["ask"]["options"] == ["resume", "start over", "drop"]
    assert {"ask-user", f"plan:{root}"} <= _open_tags(store, ask)
    body = next(
        c.text
        for c in store.chunks.list_chunks_for_ref(ask)
        if c.chunk_kind == "todo_body"
    )
    # names the state and the parked branch/host
    assert f"td{build} STATUS:open [waiting-for:coordinator]" in body
    assert "`build-host-1`" in body and "/srv/wt/slice-td7" in body
    assert ("ab" * 6) in body and f"refs/factory/slice/{build}" in body
    # the build is blocked-by the ask: answering it re-arms the build
    assert ask in {
        link.dst_ref_id
        for link in store.links_for(build, direction="out", relation="blocked-by")
    }
    # counted on the gripe, but no hold: the open ask is the hold
    assert "chain-stalls:1" in _open_tags(store, a)
    assert "hold:no-dispatch" not in _open_tags(store, a)
    assert any("ask td" in c for c in _comments(store, a))
    texts = [c.text for c in store.chunks.list_chunks_for_ref(root)]
    assert any("chain stalled" in t and f"ask td{ask}" in t for t in texts)


def test_a_second_sweep_with_the_ask_open_does_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _one_lane(monkeypatch, cap=2)
    _light(store)
    a = _gripe(store, "a")
    root, build = _chain_for(store, a)
    _park(store, build, "paused")
    _age_node(store, build, 9)
    first = gd.run_gripe_dispatch_pass(store, host="h1")
    [ask] = _asks_on(store, root)
    assert [z["ask"] for z in first.extra["stalled"]] == [ask]
    _age_node(store, build, 9)  # even long after: the open ask shields it
    for _ in range(2):
        again = gd.run_gripe_dispatch_pass(store, host="h1")
        assert again.extra["stalled"] == []
    assert _asks_on(store, root) == [ask]
    assert "chain-stalls:1" in _open_tags(store, a)
    assert _status(store, root) != "won't-do"


def test_sweep_leaves_recent_and_asked_chains_alone(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _one_lane(monkeypatch, cap=3)
    _light(store)
    recent = _gripe(store, "recent", prio=1)
    asked = _gripe(store, "asked", prio=2)
    r_root, r_build = _chain_for(store, recent)
    a_root, a_build = _chain_for(store, asked)
    for build in (r_build, a_build):
        _park(store, build, "paused")
    _age_node(store, a_build, 48)
    _age_node(store, r_build, 1)
    ask = _ask_in_plan(store, a_root)
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert res.extra["stalled"] == []
    assert _status(store, r_root) != "won't-do"
    assert _status(store, a_root) != "won't-do"
    # a closed ask no longer shields the chain: it is asked about again
    store.add_tag(
        ask, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
    )
    _age_node(store, a_build, 48)  # the ask's own stamps count as plan activity
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert [z["root"] for z in res.extra["stalled"]] == [a_root]
    assert _status(store, a_root) != "won't-do"  # asked, never closed
    assert _status(store, r_root) != "won't-do"


def test_stall_window_is_a_setting(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _one_lane(monkeypatch, cap=3)
    _light(store)
    a = _gripe(store, "a")
    root, build = _chain_for(store, a)
    _park(store, build, "paused")
    _age_node(store, build, 3)
    assert gd.stall_hours(store) == 6
    assert gd.run_gripe_dispatch_pass(store, host="h1").extra["stalled"] == []
    psettings.set_setting(gd.STALL_HOURS_KEY, "2", store=store)
    assert gd.stall_hours(store) == 2
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert [z["root"] for z in res.extra["stalled"]] == [root]


def test_every_stall_counts_but_never_holds_the_gripe(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The count stays (``chain-stalls:<n>``, replaced per ask) but the auto
    ``hold:no-dispatch`` at the second stall is gone: the open ask keeps
    the chain out of dispatch, and nothing is closed or re-minted."""
    _one_lane(monkeypatch, cap=2)
    _light(store)
    a = _gripe(store, "a")
    root, build = _chain_for(store, a)
    _park(store, build, "paused")
    _age_node(store, build, 8)
    res = gd.run_gripe_dispatch_pass(store, host="h1")  # stall 1: asked
    assert res.extra["stalled"][0]["stalls"] == 1
    [ask1] = _asks_on(store, root)
    store.add_tag(
        ask1, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
    )  # answered ("resume"), the chain stays parked and idle
    _age_node(store, build, 8)
    res = gd.run_gripe_dispatch_pass(store, host="h1")  # stall 2: asked again
    assert res.extra["stalled"][0]["stalls"] == 2
    tags = _open_tags(store, a)
    assert "chain-stalls:2" in tags and "chain-stalls:1" not in tags
    assert "hold:no-dispatch" not in tags
    assert not any("needs a re-brief" in c for c in _comments(store, a))
    assert len(_asks_on(store, root)) == 2 and len(_roots_for(store, a)) == 1
    assert res.ok == 0 and _status(store, root) != "won't-do"


def test_sweep_is_capped_per_pass(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _one_lane(monkeypatch, cap=30)
    _light(store)
    for i in range(12):
        g = _gripe(store, f"g{i}", tags=("fix:code", "hold:no-dispatch"))
        _root, build = _chain_for(store, g)
        _park(store, build, "paused")
        _age_node(store, build, 9)
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    # ten asked per pass; all twelve stay parked (an ask is not a closure)
    assert len(res.extra["stalled"]) == 10 and res.extra["parked"] == 12


def test_build_blocked_on_a_live_upstream_node_is_not_swept(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stalled is a property of the whole plan: a parked build whose
    upstream node (a review, a brief) is still live is waiting, however
    old the build's own stamps are."""
    _one_lane(monkeypatch, cap=3)
    _light(store)
    a = _gripe(store, "alpha")
    root, build = _chain_for(store, a)
    up = store.insert_ref(kind="todo", slug=None, title="upstream", parent_id=root)
    store.add_tag(up.id, Tag.open(f"plan:{root}"), set_by="system")
    store.add_tag(up.id, Tag.closed("STATUS", "open"), set_by="system")
    store.add_link(src_ref_id=build, dst_ref_id=up.id, relation="blocked-by")
    _park(store, build, "paused")
    _age_node(store, build, 48)
    [chain] = [c for c in gd.chains(store) if c.root_id == root]
    assert not chain.live and chain.plan_live  # upstream is doable
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert res.extra["stalled"] == [] and _status(store, root) != "won't-do"
    assert res.extra["parked"] == 1

    # the upstream node ends and everything is old: now the chain is stalled
    store.add_tag(
        up.id, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
    )
    with store.pool.connection() as conn:  # its queued job ends with it
        queued = [
            int(r[0])
            for r in conn.execute(
                "SELECT ref_id FROM refs WHERE kind = 'job' AND parent_id = %s",
                (up.id,),
            ).fetchall()
        ]
    for job in queued:
        store.add_tag(
            job, Tag.closed("STATUS", "cancelled"), set_by="system", replace_prefix=True
        )
    _age_node(store, build, 48)
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert [z["root"] for z in res.extra["stalled"]] == [root]
    assert _status(store, root) != "won't-do" and len(_asks_on(store, root)) == 1


def test_recent_upstream_activity_shields_a_parked_build(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Old build stamps, but another plan node moved inside the window."""
    _one_lane(monkeypatch, cap=3)
    _light(store)
    a = _gripe(store, "a")
    root, build = _chain_for(store, a)
    up = store.insert_ref(kind="todo", slug=None, title="upstream", parent_id=root)
    store.add_tag(up.id, Tag.open(f"plan:{root}"), set_by="system")
    store.add_tag(up.id, Tag.closed("STATUS", "done"), set_by="system")
    _park(store, build, "paused")
    _age_node(store, build, 48)
    with store.pool.connection() as conn:  # the finished upstream node: 1 h old
        conn.execute(
            "UPDATE ref_tags SET created_at = now() - interval '1 hour' "
            "WHERE ref_id = %s",
            (up.id,),
        )
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert res.extra["stalled"] == [] and _status(store, root) != "won't-do"


@pytest.mark.parametrize("change", ["rearm", "dispatch", "activity", "ask", "closed"])
def test_stall_ask_rechecks_after_census(
    store: Store, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from precis.workers._mint import mint_background_job

    _one_lane(monkeypatch)
    _light(store)
    gripe = _gripe(store, "race")
    root, build = _chain_for(store, gripe)
    _park(store, build, "waiting-for:coordinator")
    _age_node(store, build, 8)
    ask_about = gd._ask_stalled

    def changed_before_close(
        store: Store, chain: gd.Chain, idle: float, hours: int
    ) -> dict[str, Any] | None:
        if change == "rearm":
            store.remove_tag(build, Tag.open("waiting-for:coordinator"))
        elif change == "dispatch":
            with store.tx() as conn:
                mint_background_job(
                    store,
                    conn=conn,
                    job_type="run_slice",
                    executor="ssh_node",
                    title="run_slice",
                    params={"todo_id": build},
                    parent_id=build,
                    status="queued",
                )
        elif change == "activity":
            store.add_tag(root, Tag.open("recent-activity"), set_by="system")
        elif change == "ask":
            _ask_in_plan(store, root)
            _age_node(store, build, 8)  # Ask alone protects even an old plan.
        else:
            store.add_tag(
                root,
                Tag.closed("STATUS", "done"),
                set_by="system",
                replace_prefix=True,
            )
        return ask_about(store, chain, idle, hours)

    monkeypatch.setattr(gd, "_ask_stalled", changed_before_close)
    result = gd.run_gripe_dispatch_pass(store, host="h1")
    assert result.extra["stalled"] == []
    assert _status(store, root) != "won't-do"
    assert not any(t.startswith(gd.STALL_TAG_PREFIX) for t in _open_tags(store, gripe))
    assert not _asks_on(store, root)  # no ask raised on the changed plan
    # A root closed under the sweep frees its gripe to re-enter dispatch
    # (the minted second chain); every other change leaves the one chain.
    assert len(_roots_for(store, gripe)) == (2 if change == "closed" else 1)


def test_stall_ask_skips_while_plan_tick_holds_lock(store: Store) -> None:
    from precis.store.advisory import try_xact_advisory_lock
    from precis.workers.job_types.plan_dispatch import TICK_LOCK_KEY

    gripe = _gripe(store, "locked")
    root, build = _chain_for(store, gripe)
    _park(store, build, "paused")
    _age_node(store, build, 8)
    parked = gd.chains(store)
    assert store.dsn is not None
    with try_xact_advisory_lock(store.dsn, TICK_LOCK_KEY) as got:
        assert got
        assert gd.sweep_stalled(store, parked) == []
    assert _status(store, root) == "open"
    assert gd.sweep_stalled(store, parked)[0]["root"] == root


def test_deny_path_evidence_sentence_mints_build(store: Store, two_lanes: Any) -> None:
    """gr702295: a harness path opening evidence is no declared write set."""
    _light(store)
    gid = _gripe(
        store,
        "scripts/ship cannot land a branch, says the guard log. Repair release_deploy.py.",
    )
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert "routed" not in res.extra
    assert [m["gripe"] for m in res.extra["minted"]] == [gid]


def _todo_source(
    store: Store,
    title: str = "a source todo",
    *,
    tags: tuple[str, ...] = ("factory:build",),
) -> int:
    """A plain open todo, tagged for the factory — the source of a todo chain."""
    ref = store.insert_ref(kind="todo", slug=None, title=title)
    store.add_tag(ref.id, Tag.closed("STATUS", "open"), set_by="agent")
    for t in tags:
        store.add_tag(ref.id, Tag.open(t), set_by="agent")
    return int(ref.id)


def _todo_chain_for(store: Store, source: int) -> tuple[int, int]:
    """A todo chain: a plan root ``related-to`` ``source`` carrying
    ``meta.source_todo``. Nothing mints one yet, so the chain is a gripe
    chain (of a throwaway gripe held out of dispatch) re-pointed at the
    source; returns ``(root, build node)``."""
    scratch = _gripe(store, "scratch", tags=("fix:code", "hold:no-dispatch"))
    root, build = _chain_for(store, scratch)
    store.remove_link(src_ref_id=root, dst_ref_id=scratch, relation="related-to")
    store.add_link(src_ref_id=root, dst_ref_id=source, relation="related-to")
    store.stamp_ref_meta(root, {"source_todo": source})
    return root, build


def test_todo_chain_in_inventory_with_source(store: Store) -> None:
    source = _todo_source(store)
    root, build = _todo_chain_for(store, source)
    [chain] = [c for c in gd.chains(store) if c.root_id == root]
    assert chain.todo_ids == (source,) and chain.gripe_ids == ()
    assert chain.build_ids == (build,)
    assert (root, (), (source,), (build,)) in gd._unlanded_chains(store)
    # a chain with a closed build is no longer unlanded
    store.add_tag(
        build, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
    )
    assert root not in {c.root_id for c in gd.chains(store)}


def test_related_todo_without_source_key_is_not_a_chain(store: Store) -> None:
    source = _todo_source(store)
    other = _todo_source(store, "another todo")
    # the key names another todo than the one the root is related to
    root, _build = _todo_chain_for(store, source)
    store.stamp_ref_meta(root, {"source_todo": other})
    assert root not in {c.root_id for c in gd.chains(store)}
    # no key at all: the related-to link alone makes no chain
    root2, _build2 = _todo_chain_for(store, source)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta - 'source_todo' WHERE ref_id = %s", (root2,)
        )
    assert root2 not in {c.root_id for c in gd.chains(store)}


def test_gripe_rows_unchanged(store: Store) -> None:
    g = _gripe(store, "a")
    root, build = _chain_for(store, g)
    other = _todo_source(store)
    # a related-to link to an unrelated todo leaves the gripe row alone
    store.add_link(src_ref_id=root, dst_ref_id=other, relation="related-to")
    assert gd._unlanded_chains(store) == [(root, (g,), (), (build,))]
    [chain] = gd.chains(store)
    assert (chain.root_id, chain.gripe_ids, chain.todo_ids) == (root, (g,), ())
    assert chain.build_ids == (build,)
    # a todo chain adds its own row without touching the gripe's
    t_root, t_build = _todo_chain_for(store, other)
    rows = gd._unlanded_chains(store)
    assert (root, (g,), (), (build,)) in rows
    assert (t_root, (), (other,), (t_build,)) in rows


def test_stalled_todo_chain_gets_one_ask(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _one_lane(monkeypatch, cap=2)
    _light(store)
    source = _todo_source(store)
    root, build = _todo_chain_for(store, source)
    _park(store, build, "paused")
    _age_node(store, build, 7)
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    [z] = res.extra["stalled"]
    assert z["root"] == root and z["todos"] == [source] and z["gripes"] == []
    assert z["stalls"] == 1
    [ask] = _asks_on(store, root)
    assert z["ask"] == ask
    ref = store.get_ref(kind="todo", id=ask)
    assert ref is not None
    assert ref.meta["ask"]["options"] == ["resume", "start over", "drop"]
    body = next(
        c.text
        for c in store.chunks.list_chunks_for_ref(ask)
        if c.chunk_kind == "todo_body"
    )
    assert f"source todo td{source}" in body and "gripe" not in body
    # the marker and the comment go on the source todo
    assert "chain-stalls:1" in _open_tags(store, source)
    texts = [c.text for c in store.chunks.list_chunks_for_ref(source)]
    assert any("stalled" in t and f"ask td{ask}" in t for t in texts)
    # one ask: the open ask shields the chain from the next sweep
    _age_node(store, build, 9)
    again = gd.run_gripe_dispatch_pass(store, host="h1")
    assert again.extra["stalled"] == [] and _asks_on(store, root) == [ask]


def test_queued_todo_builds_do_not_reduce_gripe_slots(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _one_lane(monkeypatch)
    _light(store)
    source = _todo_source(store)
    _root, build = _todo_chain_for(store, source)
    _queue_build(store, build)
    free, queued = gd.build_queue_supply(store)
    assert queued == 1 and free > 0
    assert gd.build_queue_supply(store, exclude_todo_chains=True) == (free, 0)
    # the gripe pass reads the supply without the todo build: its slot stays
    g = _gripe(store, "a", prio=1)
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert res.ok == 1 and _roots_for(store, g)
    assert res.extra["queued_builds"] == 0
    # a queued gripe build still counts under the exclusion
    _queue_build(store, _build_node(store, _roots_for(store, g)[0]))
    assert gd.build_queue_supply(store, exclude_todo_chains=True) == (free, 1)
    assert gd.build_queue_supply(store) == (free, 2)


def test_queue_supply_exclusion_validates_the_chain(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``exclude_todo_chains`` queue count validates the plan root and
    its related-to edge, like the inventory: a mismatched ``source_todo``
    meta — or the key on a non-root node — excludes nothing."""
    _one_lane(monkeypatch)
    _light(store)
    source = _todo_source(store)
    other = _todo_source(store, "another todo")
    root, build = _todo_chain_for(store, source)
    _queue_build(store, build)
    free, queued = gd.build_queue_supply(store)
    assert queued == 1
    # the valid shape excludes the queued todo build
    assert gd.build_queue_supply(store, exclude_todo_chains=True) == (free, 0)
    # the key names another todo than the root is related to: still counted
    store.stamp_ref_meta(root, {"source_todo": other})
    assert gd.build_queue_supply(store, exclude_todo_chains=True) == (free, 1)
    # the related-to edge is gone but the meta remains: still counted
    store.remove_link(src_ref_id=root, dst_ref_id=source, relation="related-to")
    assert gd.build_queue_supply(store, exclude_todo_chains=True) == (free, 1)
    # the key sits on the build node, which is no plan root: still counted
    store.stamp_ref_meta(build, {"source_todo": source})
    assert gd.build_queue_supply(store, exclude_todo_chains=True) == (free, 1)


def test_parked_todo_chains_do_not_trip_gripe_backstop(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gripes keep priority: a parked todo chain is reported apart and
    never counts toward ``max_parked``."""
    _one_lane(monkeypatch, cap=3)
    _light(store)
    psettings.set_setting(gd.MAX_PARKED_KEY, "1", store=store)
    source = _todo_source(store)
    _root, build = _todo_chain_for(store, source)
    _park(store, build, "paused")
    g = _gripe(store, "a", prio=1)
    assert gd.parked_chain_count(store) == 0 and gd.open_chain_count(store) == 0
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert res.ok == 1 and _roots_for(store, g)
    assert res.extra["parked"] == 0 and res.extra["max_parked"] == 1
    assert (res.extra["todo_chains"], res.extra["todo_parked"]) == (1, 1)


def test_pass_reports_parked_full_and_picked(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the todo pass reads: ``picked`` counts the non-waiver gripes the
    pass found (before minting), ``parked_full`` the backstop."""
    _one_lane(monkeypatch, cap=3)
    _light(store)
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert res.extra["parked_full"] is False and res.extra["picked"] == 0
    assert res.extra["slots"] > 0
    _gripe(store, "a", prio=1)
    _gripe(store, "b", prio=2)
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert (
        res.extra["picked"] == 2 and res.ok == 2 and res.extra["parked_full"] is False
    )
    # a backstop of one parked chain: the pass looks at no gripe
    psettings.set_setting(gd.MAX_PARKED_KEY, "1", store=store)
    parked = _gripe(store, "parked", prio=1)
    _root, build = _chain_for(store, parked)
    _park(store, build, "paused")
    _gripe(store, "c", prio=1)
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert res.extra["parked_full"] is True and res.extra["picked"] == 0
