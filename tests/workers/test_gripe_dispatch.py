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
    with store.pool.connection() as conn:
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
        models=("claude-sonnet-5",),
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
    # the minted chain holds a lane slot like a fix:code one (capacity
    # count and the build's class agree)
    assert gd.open_chain_count(store) == 1


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
    assert gd.open_chain_count(store) == 3
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert res.extra["slots"] == 0 and res.claimed == 0


def test_pass_mints_within_free_slots_and_is_idempotent(
    store: Store, two_lanes: Any
) -> None:
    _light(store)
    a = _gripe(store, "a", prio=1)
    b = _gripe(store, "b", prio=2)
    c = _gripe(store, "c", prio=3)
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
    assert gd.open_chain_count(store) == 3

    # Second fire: all three are linked (skipped) and no slot is free.
    _gripe(store, "d", prio=1)
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


@pytest.mark.parametrize("project_tags", [(), ("project:precis",)])
def test_supported_or_default_project_dispatches(
    store: Store, two_lanes: Any, project_tags: tuple[str, ...]
) -> None:
    _light(store)
    gid = _gripe(store, "precis code bug", tags=("fix:code", *project_tags))
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
            models=("claude-sonnet-5",),
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
    assert gd.open_chain_count(store) == 9
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
    assert gd.run_gripe_dispatch_pass(store, host="h1").ok == 0  # full
    store.add_tag(
        root, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
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
    assert gd.open_chain_count(store) == 1
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert res.claimed == 0 and len(_roots_for(store, a)) == 1


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
