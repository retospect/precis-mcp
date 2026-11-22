"""todo_dispatch: the pass that mints a chain for a todo tagged ``factory:build``."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from psycopg import Connection

from precis import settings as psettings
from precis.cli import logs as cli_logs
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.job import JobHandler
from precis.plans import governs as _governs
from precis.plans import route as _route
from precis.plans import templates as T
from precis.plans.todo_chain import (
    CLOSED_BUILT_TAG,
    FACTORY_BUILD_TAG,
    NEEDS_SPEC_TAG,
    REFUSAL_KEY,
    SKIP_TAG,
    SPLIT_PARENT_TAG,
    Split,
    SplitPart,
    close_built,
    done_note,
    refuse_source,
    split_source,
)
from precis.store import Store
from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.utils.db_log_handler import BufferedDBLogHandler
from precis.workers import gripe_dispatch as gd
from precis.workers import scheduler
from precis.workers import todo_dispatch as td
from precis.workers.runner import BatchResult
from tests.conftest import _active_dsn

_ASK = {"kind": "ruling", "options": ["ok"], "blocks": "nothing", "context": "x"}


@pytest.fixture(autouse=True)
def _fresh_settings() -> Any:
    """The switch and the base ride the 60 s settings cache; keep one
    test's value out of the next."""
    for key in (td.ENABLED_KEY, td.MAX_OPEN_KEY):
        psettings.invalidate(key)
    yield
    for key in (td.ENABLED_KEY, td.MAX_OPEN_KEY):
        psettings.invalidate(key)


def _set(store: Store, key: str, value: Any) -> None:
    psettings.set_setting(key, value, store=store)
    psettings.invalidate(key)


def _on(store: Store, base: int | None = None) -> None:
    _set(store, td.ENABLED_KEY, True)
    if base is not None:
        _set(store, td.MAX_OPEN_KEY, base)


def _age(store: Store, ref_id: int, minutes: float) -> None:
    """Make everything that was written on the todo ``minutes`` old."""
    secs = minutes * 60
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET updated_at = now() - make_interval(secs => %s) "
            "WHERE ref_id = %s",
            (secs, ref_id),
        )
        conn.execute(
            "UPDATE ref_tags SET created_at = now() - make_interval(secs => %s) "
            "WHERE ref_id = %s",
            (secs, ref_id),
        )
        conn.execute(
            "UPDATE chunks SET created_at = now() - make_interval(secs => %s) "
            "WHERE ref_id = %s",
            (secs, ref_id),
        )


def _todo(
    store: Store,
    title: str = "Make the widget stop leaking",
    *,
    tags: tuple[str, ...] = (FACTORY_BUILD_TAG,),
    status: str = "open",
    prio: int | None = None,
    age_minutes: float = 120.0,
    body: str | None = "The widget leaks under load.",
    meta: dict[str, Any] | None = None,
    parent_id: int | None = None,
) -> int:
    ref = store.insert_ref(
        kind="todo", slug=None, title=title, meta=meta or {}, parent_id=parent_id
    )
    store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="agent")
    for t in tags:
        store.add_tag(ref.id, Tag.open(t), set_by="agent")
    if body is not None:
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": TODO_BODY_KIND})],
        )
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - make_interval(secs => %s), "
            "prio = %s WHERE ref_id = %s",
            (age_minutes * 60, prio, ref.id),
        )
    _age(store, int(ref.id), age_minutes)
    return int(ref.id)


def _run(
    store: Store, *, free: int = 0, idle: bool | None = None, lit: bool = True
) -> BatchResult:
    offer = td.GripeOffer(lit=lit, idle=free > 0 if idle is None else idle, free=free)
    return td.run_todo_dispatch_pass(store, offer=offer)


def _roots(store: Store, source: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT l.src_ref_id FROM links l JOIN refs t ON t.ref_id = l.src_ref_id "
            "WHERE l.dst_ref_id = %s AND l.relation = 'related-to' "
            "AND t.kind = 'todo' ORDER BY 1",
            (source,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _comments(store: Store, ref_id: int) -> list[str]:
    return [
        str(c.text)
        for c in sorted(store.chunks.list_chunks_for_ref(ref_id), key=lambda c: c.ord)
        if c.chunk_kind == "todo_comment"
    ]


def _tags(store: Store, ref_id: int) -> set[str]:
    return {str(t.value) for t in store.tags_for(ref_id) if t.namespace == "open"}


def _status(store: Store, ref_id: int) -> str | None:
    for t in store.tags_for(ref_id):
        if t.prefix == "STATUS":
            return str(t.value)
    return None


def _close(store: Store, ref_id: int, status: str) -> None:
    store.add_tag(
        ref_id, Tag.closed("STATUS", status), set_by="system", replace_prefix=True
    )


def _count_refs(store: Store) -> int:
    with store.pool.connection() as conn:
        row = conn.execute("SELECT count(*) FROM refs").fetchone()
    assert row is not None
    return int(row[0])


def _gripe_result(**extra: Any) -> BatchResult:
    return BatchResult(handler="gripe_dispatch", claimed=0, ok=0, failed=0, extra=extra)


# -- the offer and the budget -------------------------------------------------


def test_offer_from_the_gripe_pass_result() -> None:
    dark = BatchResult(handler="gripe_dispatch", claimed=0, ok=0, failed=0)
    assert td.offer_from(dark) == td.GripeOffer(False, False, 0)
    read_only = _gripe_result(read_only="PRECIS_FACTORY=off")
    assert td.offer_from(read_only) == td.GripeOffer(False, False, 0)
    # room, looked, found no eligible gripe: idle, offers its slots
    idle = _gripe_result(slots=4, picked=0, parked_full=False)
    assert td.offer_from(idle) == td.GripeOffer(True, True, 4)
    # an eligible gripe: not idle
    busy = _gripe_result(slots=4, picked=1, parked_full=False)
    assert td.offer_from(busy) == td.GripeOffer(True, False, 0)
    # no slots: the pass did not look, so it is not idle
    none = _gripe_result(slots=0, picked=0, parked_full=False)
    assert td.offer_from(none) == td.GripeOffer(True, False, 0)
    full = _gripe_result(slots=4, picked=0, parked_full=True)
    assert td.offer_from(full) == td.GripeOffer(True, False, 0)


def test_budget_is_base_plus_free_less_open() -> None:
    lit = td.GripeOffer(True, False, 0)
    assert td.budget(3, lit, 0) == 3
    assert td.budget(3, lit, 5) == 0
    assert td.budget(3, td.GripeOffer(True, True, 2), 4) == 1
    assert td.budget(0, lit, 0) == 0


def test_max_open_defaults_and_negative_reads_as_zero(store: Store) -> None:
    assert td.max_open(store) == 3
    assert not td.todo_dispatch_enabled(store)
    _set(store, td.MAX_OPEN_KEY, -4)
    assert td.max_open(store) == 0
    _set(store, td.MAX_OPEN_KEY, 7)
    assert td.max_open(store) == 7
    _on(store)
    assert td.todo_dispatch_enabled(store)


# -- the switch and the offer -------------------------------------------------


def test_switch_off_reads_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _todo(store)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the pass touched the graph with the switch off")

    for name in (
        "survey",
        "sweep_refusals",
        "sweep_ended",
        "chains",
        "mint_todo_build",
    ):
        monkeypatch.setattr(td, name, boom)
    before = _count_refs(store)
    res = _run(store, free=3)
    assert (res.handler, res.claimed, res.ok, res.failed) == ("todo_dispatch", 0, 0, 0)
    assert not res.extra
    assert _count_refs(store) == before and _roots(store, src) == []


def test_gripe_pass_not_lit_mints_nothing(store: Store) -> None:
    _on(store)
    src = _todo(store)
    res = td.run_todo_dispatch_pass(
        store,
        offer=td.offer_from(
            BatchResult(handler="gripe_dispatch", claimed=0, ok=0, failed=0)
        ),
    )
    assert res.extra["gripe_pass"] == "not lit" and res.ok == 0
    assert _roots(store, src) == []


def test_read_only_mints_nothing(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    _on(store)
    src = _todo(store)
    monkeypatch.setenv("PRECIS_FACTORY", "off")
    res = _run(store)
    assert res.extra["read_only"] == "PRECIS_FACTORY=off"
    assert _roots(store, src) == []


# -- eligibility --------------------------------------------------------------


def test_untagged_never_minted(store: Store) -> None:
    _on(store)
    plain = _todo(store, "no tag at all", tags=())
    other = _todo(store, "some other tag", tags=("fix:code",))
    eligible, skipped = td.survey(store)
    assert eligible == [] and skipped == []
    res = _run(store)
    assert res.ok == 0 and res.extra["minted"] == []
    assert _roots(store, plain) == [] and _roots(store, other) == []


def _src_not_open(store: Store) -> int:
    return _todo(store, status="done")


def _src_plan_node(store: Store) -> int:
    return _todo(store, tags=(FACTORY_BUILD_TAG, "plan:999999"))


def _src_ask(store: Store) -> int:
    return _todo(store, meta={"ask": _ASK})


def _src_open_child(store: Store) -> int:
    src = _todo(store)
    _todo(store, "a child", tags=(), parent_id=src)
    return src


def _src_blocked(store: Store) -> int:
    src = _todo(store)
    blocker = _todo(store, "the blocker", tags=())
    store.add_link(src_ref_id=src, dst_ref_id=blocker, relation="blocked-by")
    return src


def test_survey_respects_private_blocker(store: Store) -> None:
    from precis.store.revision_context import revision_context
    from precis.todo.blocked import open_blocked_by

    src = _todo(store)
    blocker = _todo(store, "private dependency", tags=())
    store.add_tag(blocker, Tag.open("user_secret:alice"))
    store.add_link(src_ref_id=src, dst_ref_id=blocker, relation="blocked-by")
    with revision_context(actor="user:bob"):
        assert open_blocked_by(store, [src]) == {}
        eligible, skipped = td.survey(store)
        assert eligible == []
        assert skipped == [{"todo": src, "reason": "blocked"}]


def _src_hold_tag(store: Store) -> int:
    return _todo(store, tags=(FACTORY_BUILD_TAG, "hold:no-dispatch"))


def _src_needs_spec(store: Store) -> int:
    return _todo(store, tags=(FACTORY_BUILD_TAG, NEEDS_SPEC_TAG))


def _src_skip_tag(store: Store) -> int:
    return _todo(store, tags=(FACTORY_BUILD_TAG, SKIP_TAG))


def _src_chained(store: Store) -> int:
    src = _todo(store)
    T.mint_todo_build(store, todo_id=src)
    return src


def _src_other_repo(store: Store) -> int:
    return _todo(store, tags=(FACTORY_BUILD_TAG, "repo:catpath"))


def _src_other_project(store: Store) -> int:
    return _todo(store, tags=(FACTORY_BUILD_TAG, "project:catpath"))


def _src_settling(store: Store) -> int:
    return _todo(store, age_minutes=2)


_SKIP_CASES = {
    "not-open": _src_not_open,
    "plan-node": _src_plan_node,
    "ask": _src_ask,
    "open-child": _src_open_child,
    "blocked": _src_blocked,
    "hold-tag": _src_hold_tag,
    "needs-spec": _src_needs_spec,
    "skip-tag": _src_skip_tag,
    "chained": _src_chained,
    "other-repo": _src_other_repo,
    "settling": _src_settling,
}


@pytest.mark.parametrize("reason", sorted(_SKIP_CASES))
def test_each_skip_reason_is_named_in_the_report(store: Store, reason: str) -> None:
    ok = _todo(store, "the one that is fine")
    src = _SKIP_CASES[reason](store)
    eligible, skipped = td.survey(store)
    assert [p.todo_id for p in eligible] == [ok]
    assert skipped == [{"todo": src, "reason": reason}]
    _on(store)
    res = _run(store)
    assert {"todo": src, "reason": reason} in res.extra["skipped"]
    assert [m["todo"] for m in res.extra["minted"]] == [ok]


def test_other_project_is_other_repo(store: Store) -> None:
    src = _src_other_project(store)
    assert td.survey(store) == ([], [{"todo": src, "reason": "other-repo"}])


def test_default_repo_and_in_repo_projects_are_eligible(store: Store) -> None:
    """gr752647: a ``project:`` tag names an area of this repository unless
    it is one the gripe dispatcher knows to live elsewhere."""
    a = _todo(store, "a", tags=(FACTORY_BUILD_TAG, "repo:precis"))
    b = _todo(store, "b", tags=(FACTORY_BUILD_TAG, "project:precis-mcp"))
    c = _todo(store, "c", tags=(FACTORY_BUILD_TAG, "project:platform"))
    eligible, skipped = td.survey(store)
    assert {p.todo_id for p in eligible} == {a, b, c} and skipped == []


def test_a_closed_child_does_not_hold_the_source(store: Store) -> None:
    src = _todo(store)
    child = _todo(store, "done child", tags=(), parent_id=src, status="done")
    assert child
    eligible, _ = td.survey(store)
    assert [p.todo_id for p in eligible] == [src]


def test_other_repo_comment_once(store: Store) -> None:
    _on(store)
    src = _src_other_repo(store)
    for _ in range(2):
        res = _run(store)
        assert res.ok == 0 and res.extra["minted"] == []
        assert _roots(store, src) == []
    [note] = _comments(store, src)
    assert note.startswith(td.OTHER_REPO_PREFIX) and "No chain minted" in note


def test_tag_removed_before_mint(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner withdraws the tag between the survey and the mint."""
    _on(store)
    src = _todo(store)
    real = td.fenced_refusal

    def withdraw(st: Store, pick: td.TodoPick) -> Any:
        store.remove_tag(src, Tag.open(FACTORY_BUILD_TAG))
        return real(st, pick)

    monkeypatch.setattr(td, "fenced_refusal", withdraw)
    res = _run(store)
    assert res.ok == 0 and res.failed == 0 and res.extra["minted"] == []
    [skip] = [s for s in res.extra["skipped"] if s["todo"] == src]
    # td752028: the report's reason is a stable vocabulary word; the
    # BadInput message — which names the withdrawn tag — rides in `detail`.
    assert skip["reason"] == "mint-refused"
    assert FACTORY_BUILD_TAG in skip["detail"]
    assert _roots(store, src) == []


# -- budget and order ---------------------------------------------------------


def test_base_cap_with_eligible_gripes(store: Store) -> None:
    """An eligible gripe means no free offer: the pass starts its base only."""
    _on(store, base=2)
    srcs = [_todo(store, f"t{i}") for i in range(5)]
    offer = td.offer_from(_gripe_result(slots=4, picked=2, parked_full=False))
    res = td.run_todo_dispatch_pass(store, offer=offer)
    assert (res.extra["base"], res.extra["free"], res.extra["budget"]) == (2, 0, 2)
    assert len(res.extra["minted"]) == 2 and res.ok == 2
    again = td.run_todo_dispatch_pass(store, offer=offer)
    assert again.extra["open_todo"] == 2 and again.extra["budget"] == 0
    assert again.extra["minted"] == []
    assert {
        item["todo"] for item in again.extra["skipped"] if item["reason"] == "budget"
    } == set(srcs[2:])
    assert sum(len(_roots(store, s)) for s in srcs) == 2


def test_grows_into_free_slots_only(store: Store) -> None:
    _on(store, base=1)
    srcs = [_todo(store, f"t{i}") for i in range(6)]
    idle = td.offer_from(_gripe_result(slots=2, picked=0, parked_full=False))
    assert idle.free == 2
    res = td.run_todo_dispatch_pass(store, offer=idle)
    assert res.extra["budget"] == 3 and len(res.extra["minted"]) == 3
    # the offer is not banked: with the three chains open, nothing more
    res = td.run_todo_dispatch_pass(store, offer=idle)
    assert (res.extra["open_todo"], res.extra["budget"]) == (3, 0)
    assert res.extra["minted"] == []
    # nor does a busy gripe pass cancel the chains already started
    busy = td.offer_from(_gripe_result(slots=2, picked=1, parked_full=False))
    res = td.run_todo_dispatch_pass(store, offer=busy)
    assert res.extra["budget"] == 0 and res.extra["open_todo"] == 3
    assert sum(len(_roots(store, s)) for s in srcs) == 3


def test_no_growth_when_gripe_pass_had_no_slots(store: Store) -> None:
    _on(store, base=1)
    for i in range(4):
        _todo(store, f"t{i}")
    offer = td.offer_from(_gripe_result(slots=0, picked=0, parked_full=False))
    assert not offer.idle and offer.free == 0
    res = td.run_todo_dispatch_pass(store, offer=offer)
    assert res.extra["budget"] == 1 and len(res.extra["minted"]) == 1


def test_mint_batch_cap(store: Store) -> None:
    _on(store, base=10)
    srcs = [_todo(store, f"t{i}") for i in range(5)]
    res = _run(store)
    assert res.extra["budget"] == 10 and len(res.extra["minted"]) == td._MINT_BATCH == 3
    assert res.extra["skipped"] == [
        {"todo": src, "reason": "batch-cap"} for src in srcs[3:]
    ]
    res = _run(store)
    assert len(res.extra["minted"]) == 2


def test_order_prio_then_age(store: Store) -> None:
    low_old = _todo(store, "low old", prio=5, age_minutes=900)
    hot_new = _todo(store, "hot new", prio=1, age_minutes=200)
    hot_old = _todo(store, "hot old", prio=1, age_minutes=400)
    none_prio = _todo(store, "no prio", prio=None, age_minutes=300)
    eligible, _ = td.survey(store)
    # prio 1 first (older first), then the unset prio (reads 5) with the
    # prio-5 todo by age
    assert [p.todo_id for p in eligible] == [hot_old, hot_new, low_old, none_prio]
    _on(store, base=2)
    res = _run(store)
    assert [m["todo"] for m in res.extra["minted"]] == [hot_old, hot_new]


def test_stalled_and_brief_stage_chains_count(store: Store) -> None:
    _on(store, base=2)
    srcs = [_todo(store, f"t{i}") for i in range(4)]
    res = _run(store)
    assert [m["todo"] for m in res.extra["minted"]] == srcs[:2]
    # both chains sit in their brief step: they still count
    res = _run(store)
    assert (res.extra["open_todo"], res.extra["budget"]) == (2, 0)
    # a stalled chain (its build parked) counts too
    root = _roots(store, srcs[0])[0]
    [chain] = [c for c in gd.chains(store) if c.root_id == root]
    store.add_tag(
        chain.build_ids[0],
        Tag.closed("STATUS", "paused"),
        set_by="system",
        replace_prefix=True,
    )
    res = _run(store)
    assert (res.extra["open_todo"], res.extra["budget"]) == (2, 0)
    assert res.extra["minted"] == []


# -- the fenced-path pre-check ------------------------------------------------


def _children(store: Store, parent: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'todo' AND parent_id = %s "
            "ORDER BY ref_id",
            (parent,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def test_fenced_path_refused_before_any_session(store: Store) -> None:
    _on(store)
    src = _todo(
        store,
        "Tighten the land guard",
        body="File set:\n- scripts/ship\n\nChange how the flags are read.",
    )
    ok = _todo(store, "the one that is fine")
    res = _run(store)
    [refusal] = res.extra["refused"]
    assert refusal["todo"] == src and refusal["reason"] == "fenced-path"
    assert [m["todo"] for m in res.extra["minted"]] == [ok]
    assert _roots(store, src) == []
    [ask] = _children(store, src)
    assert ask == refusal["ask"]
    ask_ref = store.get_ref(kind="todo", id=ask)
    assert ask_ref is not None
    assert ask_ref.meta[REFUSAL_KEY]["reason"] == "decide"
    assert "scripts/ship" in " ".join(
        str(c.text) for c in store.chunks.list_chunks_for_ref(ask)
    )
    assert NEEDS_SPEC_TAG in _tags(store, src)
    # the next pass reads the refused todo as held, and asks nothing more
    res = _run(store)
    assert {"todo": src, "reason": "needs-spec"} in res.extra["skipped"]
    assert _children(store, src) == [ask]


def test_graph_only_todo_is_refused_not_code(store: Store) -> None:
    _on(store)
    src = _todo(store, "me123456", body="remove the stale edge from me123456")
    res = _run(store)
    [refusal] = res.extra["refused"]
    assert (refusal["todo"], refusal["reason"]) == (src, "not-code")
    ask = store.get_ref(kind="todo", id=refusal["ask"])
    assert ask is not None and ask.meta[REFUSAL_KEY]["reason"] == "not-code"
    assert _roots(store, src) == []


def test_graph_only_prefix_matches_the_route() -> None:
    reason = _route.coordinator_route(
        "me123456 remove the stale edge from me123456", ()
    )
    assert reason is not None and reason.startswith(td._GRAPH_ONLY_PREFIX)


_SKILLS = "src/precis/data/skills/"


def _govern(monkeypatch: pytest.MonkeyPatch, *stems: str) -> None:
    paths = tuple(f"{_SKILLS}{stem}.md" for stem in stems)
    monkeypatch.setattr(_governs, "governed_skill_paths", lambda *_a, **_k: paths)


def test_governed_skill_named_by_slug_is_refused(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _govern(monkeypatch, "precis-work-help", "bug")
    slug = "precis-work-help"
    _on(store)
    src = _todo(store, "Reword the intro", body=f"Where: {slug}\nChange: shorten it.")
    res = _run(store)
    [refusal] = res.extra["refused"]
    assert (refusal["todo"], refusal["reason"]) == (src, "fenced-path")
    assert _roots(store, src) == []
    [ask] = _children(store, src)
    text = " ".join(str(c.text) for c in store.chunks.list_chunks_for_ref(ask))
    assert slug in text and "governs the factory" in text
    # a todo that only names a longer word containing the slug is no match
    other = _todo(store, "Another", body=f"Where: {slug}-extra and x{slug}")
    assert td.fenced_refusal(store, td.TodoPick(other, "Another", None)) is None
    # the slug ending a sentence is a mention; as a path stem it is the route's
    named = td.fenced_refusal(store, td.TodoPick(other, f"see {slug}.", None))
    assert named is not None and named[0] == "decide"
    assert td._governed_slug(f"{_SKILLS}{slug}.md and {slug}.md") is None


def test_common_word_matching_a_governed_stem_is_not_refused(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _govern(monkeypatch, "bug", "land", "next", "precis-work-help")
    _on(store)
    src = _todo(store, "Fix the bug", body="fix the bug and land the next step")
    pick = td.TodoPick(src, "Fix the bug", None)
    assert td.fenced_refusal(store, pick) is None
    res = _run(store)
    assert res.extra["refused"] == [] and len(_roots(store, src)) == 1


def test_fence_check_failure_fails_open(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(store)
    src = _todo(store)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("classifier blip")

    monkeypatch.setattr(td._route, "coordinator_route", boom)
    res = _run(store)
    assert [m["todo"] for m in res.extra["minted"]] == [src]
    assert res.extra["refused"] == [] and len(_roots(store, src)) == 1


# -- the refusal-answer sweep -------------------------------------------------


def _refused(store: Store, reason: str = "rewrite") -> tuple[int, int]:
    """A source refused by the brief step: ``(source, ask)``."""
    src = _todo(store)
    ask = refuse_source(store, None, src, reason, "the Where line is missing")
    return src, ask


def _answer(
    store: Store,
    ask: int,
    text: str | None,
    status: str = "done",
    *,
    aged: bool = True,
    principal: str = "user:owner",
    prefix: str = "answer: ",
) -> None:
    """Answer and close the ask; ``aged`` makes the close older than the
    answer grace time, so the sweep may read it."""
    if text is not None:
        existing = store.chunks.list_chunks_for_ref(ask)
        store.chunks.insert_chunks(
            ask,
            [
                ChunkInsert(
                    ord=max(int(c.ord) for c in existing) + 1,
                    text=f"{prefix}{text}",
                    meta={"chunk_kind": "todo_comment", "principal": principal},
                )
            ],
        )
    _close(store, ask, status)
    if aged:
        _age(store, ask, 10)


def test_open_ask_leaves_the_source_alone(store: Store) -> None:
    src, _ask = _refused(store)
    assert td.sweep_refusals(store) == []
    assert NEEDS_SPEC_TAG in _tags(store, src)


def test_rewrite_answer_makes_eligible(store: Store) -> None:
    src, ask = _refused(store)
    _answer(store, ask, "rewrite")
    assert td.sweep_refusals(store) == [{"todo": src, "ask": ask, "outcome": "rewrite"}]
    assert _tags(store, src) == {FACTORY_BUILD_TAG}
    [_refused_note, applied] = _comments(store, src)
    assert f"td{ask}" in applied and "rewrite" in applied
    # it re-enters intake after the settle time, with its new text
    assert td.survey(store) == ([], [{"todo": src, "reason": "settling"}])
    _age(store, src, 60)
    eligible, _ = td.survey(store)
    assert [p.todo_id for p in eligible] == [src]


def test_split_answer_removes_tag(store: Store) -> None:
    src, ask = _refused(store, "split")
    _answer(store, ask, "split")
    [res] = td.sweep_refusals(store)
    assert res["outcome"] == "split"
    assert _tags(store, src) == set() and _status(store, src) == "open"


def test_drop_removes_tag(store: Store) -> None:
    src, ask = _refused(store)
    _answer(store, ask, "Drop")
    [res] = td.sweep_refusals(store)
    assert res["outcome"] == "drop"
    assert _tags(store, src) == set() and _status(store, src) == "open"
    assert td.survey(store) == ([], [])


def test_close_as_done_closes_source(store: Store) -> None:
    src, ask = _refused(store, "built")
    store.add_tag(src, Tag.open("claimed-by:someone"), set_by="agent")
    _answer(store, ask, "close as done")
    [res] = td.sweep_refusals(store)
    assert res["outcome"] == "close"
    assert _status(store, src) == "done" and _tags(store, src) == set()
    assert any("closed the todo as done" in c for c in _comments(store, src))


@pytest.mark.parametrize(
    ("answer", "principal", "prefix"),
    [
        ("close as done", "slice:td123j456:brief", "answer: "),
        ("close as done", "install", "answer: "),
        ("close as done", "personal:123", "answer: "),
        ("close as done", "user:owner", ""),
        ("do not close as done", "user:owner", "answer: "),
        ("rewrite, or else split it", "user:owner", "answer: "),
    ],
)
def test_refusal_requires_explicit_owner_choice(
    store: Store, answer: str, principal: str, prefix: str
) -> None:
    src, ask = _refused(store, "built")
    _answer(store, ask, answer, principal=principal, prefix=prefix)
    assert td.sweep_refusals(store) == [{"todo": src, "ask": ask, "outcome": "unread"}]
    assert _tags(store, src) == {FACTORY_BUILD_TAG, NEEDS_SPEC_TAG}
    assert _status(store, src) == "open"


def test_ambiguous_answer_changes_nothing(store: Store) -> None:
    src, ask = _refused(store)
    _answer(store, ask, "rewrite it, or else split it, or drop it")
    assert td.sweep_refusals(store) == [{"todo": src, "ask": ask, "outcome": "unread"}]
    assert _tags(store, src) == {FACTORY_BUILD_TAG, NEEDS_SPEC_TAG}
    assert _status(store, src) == "open"
    [_refused_note, note] = _comments(store, src)
    assert note.startswith(f"dispatch: could not read an owner answer on td{ask}")
    assert "Nothing changed" in note
    ask_ref = store.get_ref(kind="todo", id=ask)
    assert ask_ref is not None
    assert ask_ref.meta[td._APPLIED_KEY] == {"outcome": "unread"}
    # said once
    assert td.sweep_refusals(store) == []
    assert len(_comments(store, src)) == 2


def test_empty_answer_changes_nothing(store: Store) -> None:
    src, ask = _refused(store)
    _answer(store, ask, None, status="won't-do")
    assert td.sweep_refusals(store) == [{"todo": src, "ask": ask, "outcome": "unread"}]
    assert _tags(store, src) == {FACTORY_BUILD_TAG, NEEDS_SPEC_TAG}
    assert _status(store, src) == "open"


def test_fresh_closed_ask_waits_for_the_grace_time(store: Store) -> None:
    src, ask = _refused(store)
    _answer(store, ask, "drop", aged=False)
    assert td.sweep_refusals(store) == []
    assert NEEDS_SPEC_TAG in _tags(store, src)
    _age(store, ask, td._ANSWER_GRACE_MINUTES + 1)
    assert [r["outcome"] for r in td.sweep_refusals(store)] == ["drop"]


def test_refusal_applied_once(store: Store) -> None:
    src, ask = _refused(store)
    _answer(store, ask, "rewrite")
    assert len(td.sweep_refusals(store)) == 1
    before = _comments(store, src)
    # the owner tags it needs-spec again by hand: the applied ask is not
    # applied a second time
    store.add_tag(src, Tag.open(NEEDS_SPEC_TAG), set_by="agent")
    assert td.sweep_refusals(store) == []
    assert _comments(store, src) == before
    ask_ref = store.get_ref(kind="todo", id=ask)
    assert ask_ref is not None and ask_ref.meta[td._APPLIED_KEY] == {
        "outcome": "rewrite"
    }


def test_sweep_runs_inside_the_pass(store: Store) -> None:
    _on(store)
    src, ask = _refused(store)
    _answer(store, ask, "drop")
    res = _run(store)
    assert res.extra["refusals_applied"] == [
        {"todo": src, "ask": ask, "outcome": "drop"}
    ]


# -- the ended-chain sweep ----------------------------------------------------


def test_dropped_chain_releases_source_once(store: Store) -> None:
    src = _todo(store)
    mint = T.mint_todo_build(store, todo_id=src)
    _close(store, mint.root_id, "won't-do")
    assert td.sweep_ended(store) == [src]
    assert FACTORY_BUILD_TAG not in _tags(store, src)
    [note] = _comments(store, src)
    assert f"td{mint.root_id}" in note and "dropped" in note
    root = store.get_ref(kind="todo", id=mint.root_id)
    assert root is not None and root.meta[td._RELEASED_KEY] == src
    # tagged again by the owner: the same dead root releases nothing
    store.add_tag(src, Tag.open(FACTORY_BUILD_TAG), set_by="agent")
    assert td.sweep_ended(store) == []
    assert FACTORY_BUILD_TAG in _tags(store, src)
    assert len(_comments(store, src)) == 1


def test_dropped_chain_not_repicked(store: Store) -> None:
    _on(store)
    src = _todo(store)
    res = _run(store)
    [minted] = res.extra["minted"]
    _close(store, minted["root"], "won't-do")
    res = _run(store)
    assert res.extra["released"] == [src] and res.extra["minted"] == []
    res = _run(store)
    assert res.extra["released"] == [] and res.extra["minted"] == []
    assert _roots(store, src) == [minted["root"]]
    assert td.survey(store) == ([], [])


def test_retagged_after_release_is_eligible_again(store: Store) -> None:
    src = _todo(store)
    mint = T.mint_todo_build(store, todo_id=src)
    _close(store, mint.root_id, "won't-do")
    assert td.sweep_ended(store) == [src]
    store.add_tag(src, Tag.open(FACTORY_BUILD_TAG), set_by="agent")
    _age(store, src, 60)
    eligible, skipped = td.survey(store)
    assert [p.todo_id for p in eligible] == [src] and skipped == []
    _on(store)
    res = _run(store)
    [new] = res.extra["minted"]
    assert new["todo"] == src and new["root"] != mint.root_id


def test_a_refusal_root_releases_nothing(store: Store) -> None:
    """The brief step's refusal closes the root ``won't-do`` too; after a
    ``rewrite`` answer the tag stays and the old root must not release it."""
    src = _todo(store)
    mint = T.mint_todo_build(store, todo_id=src)
    ask = refuse_source(store, mint.root_id, src, "rewrite", "missing Where")
    assert td.sweep_ended(store) == []
    _answer(store, ask, "rewrite")
    assert len(td.sweep_refusals(store)) == 1
    assert td.sweep_ended(store) == []
    assert _tags(store, src) == {FACTORY_BUILD_TAG}


def test_ended_root_with_a_live_chain_releases_nothing(store: Store) -> None:
    src = _todo(store)
    dead = T.mint_todo_build(store, todo_id=src)
    _close(store, dead.root_id, "won't-do")
    T.mint_todo_build(store, todo_id=src)
    assert td.sweep_ended(store) == []
    assert FACTORY_BUILD_TAG in _tags(store, src)


_SHA = "abcdef1234567"


def _note(store: Store, ref_id: int, text: str) -> None:
    existing = store.chunks.list_chunks_for_ref(ref_id)
    store.chunks.insert_chunks(
        ref_id,
        [
            ChunkInsert(
                ord=max((int(c.ord) for c in existing), default=0) + 1,
                text=text,
                meta={"chunk_kind": "todo_comment"},
            )
        ],
    )


def _land(store: Store, mint: T.PlanMint) -> None:
    """The chain landed ``_SHA`` and the tick opened its gate: the root is
    done, the source waits for the close."""
    gate = mint.ids["gate"]
    store.stamp_ref_meta(gate, {"gate": {"kind": "sha-in-prod", "args": {"sha": _SHA}}})
    _close(store, gate, "done")
    _close(store, mint.root_id, "done")


def _no_land(store: Store, mint: T.PlanMint) -> None:
    """A no-op verdict: nothing pushed, review and gate dead, root done."""
    _close(store, mint.ids["review"], "won't-do")
    _close(store, mint.ids["gate"], "won't-do")
    _close(store, mint.ids["build"], "done")
    _close(store, mint.root_id, "done")


def test_noop_chain_releases_source_and_is_not_reminted(store: Store) -> None:
    src = _todo(store)
    mint = T.mint_todo_build(store, todo_id=src)
    _no_land(store, mint)
    assert td.sweep_ended(store) == [src]
    assert FACTORY_BUILD_TAG not in _tags(store, src) and _status(store, src) == "open"
    [note] = _comments(store, src)
    assert note.startswith(f"dispatch: the chain td{mint.root_id} ended without a land")
    assert "removed `factory:build`" in note
    assert td.survey(store) == ([], [])
    _on(store)
    res = _run(store)
    assert res.extra["minted"] == [] and _roots(store, src) == [mint.root_id]
    assert td.sweep_ended(store) == []


def test_closing_source_is_skipped_not_released(store: Store) -> None:
    src = _todo(store)
    mint = T.mint_todo_build(store, todo_id=src)
    _land(store, mint)
    _note(store, src, f"landed: {_SHA} (the land)")
    assert td.sweep_ended(store) == []
    assert FACTORY_BUILD_TAG in _tags(store, src)
    assert td.survey(store) == ([], [{"todo": src, "reason": "closing"}])
    # the backstop closes it; the pass reports it
    _on(store)
    res = _run(store)
    assert res.extra["closed"] == [src] and res.extra["minted"] == []
    assert _status(store, src) == "done"


def test_landed_root_without_landed_comment_is_released(store: Store) -> None:
    src = _todo(store)
    mint = T.mint_todo_build(store, todo_id=src)
    _land(store, mint)
    # no ``landed:`` comment: close_source could never succeed
    assert td.sweep_ended(store) == [src]
    assert FACTORY_BUILD_TAG not in _tags(store, src)
    assert td.survey(store) == ([], [])


def test_reopened_source_is_not_released_by_its_old_root(store: Store) -> None:
    src = _todo(store)
    mint = T.mint_todo_build(store, todo_id=src)
    _no_land(store, mint)
    _note(store, src, done_note(_SHA, mint.root_id))
    assert td.sweep_ended(store) == []
    assert FACTORY_BUILD_TAG in _tags(store, src)


def test_tag_added_after_the_chain_ended_is_kept(store: Store) -> None:
    src = _todo(store)
    mint = T.mint_todo_build(store, todo_id=src)
    _no_land(store, mint)
    # the root ended long ago; the person tagged the todo later
    _age(store, mint.root_id, 600)
    assert td.sweep_ended(store) == []
    assert FACTORY_BUILD_TAG in _tags(store, src)


@pytest.mark.parametrize("change", ["released", "retagged", "reopened"])
def test_ended_sweep_rechecks_selected_root(
    store: Store, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    src = _todo(store)
    root = T.mint_todo_build(store, todo_id=src).root_id
    _close(store, root, "won't-do")
    with store.pool.connection() as conn:
        stale = td._ended_roots(conn)
    assert stale == [(root, src, "won't-do")]
    if change == "released":
        assert td.sweep_ended(store) == [src]
        store.add_tag(src, Tag.open(FACTORY_BUILD_TAG), set_by="agent")
    elif change == "retagged":
        store.remove_tag(src, Tag.open(FACTORY_BUILD_TAG))
        store.add_tag(src, Tag.open(FACTORY_BUILD_TAG), set_by="agent")
    else:
        _close(store, src, "done")
        _close(store, src, "open")
    before = _comments(store, src)
    real = td._ended_roots

    def survey_first(
        conn: Connection, *, root: int | None = None
    ) -> list[tuple[int, int, str]]:
        return stale if root is None else real(conn, root=root)

    monkeypatch.setattr(td, "_ended_roots", survey_first)
    assert td.sweep_ended(store) == []
    assert FACTORY_BUILD_TAG in _tags(store, src)
    assert _comments(store, src) == before


@pytest.mark.parametrize(
    "change", ["applied", "ask reopened", "source reopened", "retagged"]
)
def test_refusal_sweep_rechecks_selected_ask(store: Store, change: str) -> None:
    src, ask = _refused(store)
    _answer(store, ask, None)
    with store.pool.connection() as conn:
        assert td._answered_refusals(conn) == [(src, ask)]
    if change == "applied":
        assert len(td.sweep_refusals(store)) == 1
    elif change == "ask reopened":
        _close(store, ask, "open")
    elif change == "source reopened":
        _close(store, src, "done")
        _close(store, src, "open")
    else:
        store.remove_tag(src, Tag.open(NEEDS_SPEC_TAG))
        store.add_tag(src, Tag.open(NEEDS_SPEC_TAG), set_by="agent")
    before = _comments(store, src)
    assert td._apply_refusal(store, src, ask) is None
    assert _comments(store, src) == before


@pytest.mark.parametrize("hold", [*td.HOLD_TAGS, NEEDS_SPEC_TAG, SKIP_TAG, None])
def test_mint_rechecks_holds_added_after_survey(store: Store, hold: str | None) -> None:
    src = _todo(store)
    eligible, _ = td.survey(store)
    assert [pick.todo_id for pick in eligible] == [src]
    if hold is None:
        blocker = _todo(store, tags=())
        store.add_link(src_ref_id=src, dst_ref_id=blocker, relation="blocked-by")
        reason = "open blocker"
    else:
        store.add_tag(src, Tag.open(hold), set_by="agent")
        reason = "hold tags"
    with pytest.raises(BadInput, match=reason):
        T.mint_todo_build(store, todo_id=src)
    assert _roots(store, src) == []


def test_each_sweep_fails_on_its_own(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(store)
    src = _todo(store)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("sweep blew up")

    for name in (
        "sweep_refusals",
        "sweep_ended",
        "sweep_landed_sources",
        "sweep_splits",
    ):
        monkeypatch.setattr(td, name, boom)
    res = _run(store)
    assert [m["todo"] for m in res.extra["minted"]] == [src]
    assert (res.extra["refusals_applied"], res.extra["released"]) == ([], [])
    assert res.extra["closed"] == [] and res.extra["split_closed"] == []


# -- split sources ------------------------------------------------------------


def _split_source(store: Store, n: int = 2) -> tuple[int, list[int]]:
    """A tagged todo whose brief step split it into ``n`` parts; the parts
    are aged past the settle time."""
    src = _todo(store)
    mint = T.mint_todo_build(store, todo_id=src)
    parts = split_source(
        store,
        mint.root_id,
        src,
        mint.ids["brief"],
        Split(tuple(SplitPart(f"Part {k}", "c", "w", "d") for k in range(1, n + 1))),
    )
    for part in parts:
        _age(store, part, 120)
    return src, parts


def test_only_first_part_is_eligible(store: Store) -> None:
    src, (first, second, third) = _split_source(store, 3)
    eligible, skipped = td.survey(store)
    assert [p.todo_id for p in eligible] == [first]
    assert {"todo": second, "reason": "blocked"} in skipped
    assert {"todo": third, "reason": "blocked"} in skipped
    # the source lost the tag: it is not offered at all
    assert all(s["todo"] != src for s in skipped)
    assert src not in [p.todo_id for p in eligible]
    # the pass mints a chain for the first part only
    _on(store)
    res = _run(store)
    assert [m["todo"] for m in res.extra["minted"]] == [first]
    assert _roots(store, second) == [] and _roots(store, third) == []
    # once the first part is done the next one is offered
    _close(store, first, "done")
    eligible, _skipped = td.survey(store)
    assert [p.todo_id for p in eligible] == [second]


def test_pass_closes_split_source_when_parts_done(store: Store) -> None:
    src, (first, second) = _split_source(store)
    _on(store)
    res = _run(store)
    assert res.extra["split_closed"] == []
    _close(store, first, "done")
    assert _run(store).extra["split_closed"] == []
    _close(store, second, "done")
    res = _run(store)
    assert res.extra["split_closed"] == [src]
    assert _status(store, src) == "done"
    assert _run(store).extra["split_closed"] == []


def test_a_dropped_part_leaves_the_source_open(store: Store) -> None:
    src, (first, second) = _split_source(store)
    _close(store, first, "done")
    _close(store, second, "won't-do")
    _on(store)
    assert _run(store).extra["split_closed"] == []
    assert _status(store, src) == "open"


def test_pass_closes_a_parent_tagged_split_by_hand(store: Store) -> None:
    parent = _todo(store, "Do two things", tags=(SPLIT_PARENT_TAG,))
    first = _todo(store, "one", tags=(), parent_id=parent, status="done")
    second = _todo(store, "two", tags=(), parent_id=parent)
    _on(store)
    assert _run(store).extra["split_closed"] == []
    assert _status(store, parent) == "open"
    _close(store, second, "done")
    res = _run(store)
    assert res.extra["split_closed"] == [parent]
    assert _status(store, parent) == "done"
    assert _comments(store, parent)[-1] == (
        f"done: every part is done (td{first}, td{second})"
    )
    assert _run(store).extra["split_closed"] == []


def test_a_wont_do_child_leaves_a_tagged_parent_open(store: Store) -> None:
    parent = _todo(store, "Do two things", tags=(SPLIT_PARENT_TAG,))
    _todo(store, "one", tags=(), parent_id=parent, status="done")
    _todo(store, "two", tags=(), parent_id=parent, status="won't-do")
    _on(store)
    assert _run(store).extra["split_closed"] == []
    assert _status(store, parent) == "open"


# -- the source a built refusal closed ----------------------------------------


def test_ended_sweep_leaves_a_source_closed_as_built_alone(store: Store) -> None:
    """``close_built`` closes the root ``won't-do`` and the source ``done``:
    the ended-chain sweep must not comment on, retag or stamp either."""
    src = _todo(store)
    mint = T.mint_todo_build(store, todo_id=src)
    assert close_built(store, mint.root_id, src, mint.ids["brief"], "td9 built it")
    assert _status(store, mint.root_id) == "won't-do"
    comments, tags = _comments(store, src), _tags(store, src)
    assert td.sweep_ended(store) == []
    assert _comments(store, src) == comments
    assert _tags(store, src) == tags and CLOSED_BUILT_TAG in tags
    assert _status(store, src) == "done"
    root = store.get_ref(kind="todo", id=mint.root_id)
    assert root is not None and td._RELEASED_KEY not in root.meta
    # a whole pass leaves it alone too and offers it again to no one
    _on(store)
    res = _run(store)
    assert res.extra["released"] == [] and res.extra["minted"] == []
    assert _comments(store, src) == comments
    assert td.survey(store) == ([], [])


@pytest.mark.parametrize("drop", ["won't-do", "auto-timeout", "retired"])
def test_an_unfinished_predecessor_holds_the_next_part(store: Store, drop: str) -> None:
    """A part builds only after its predecessor is live and ``done``: a
    dropped or retired one is never skipped over — the next part reads
    ``blocked`` at survey and the mint refuses it too."""
    src, (first, second) = _split_source(store)
    if drop == "retired":
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET retired_at = now() WHERE ref_id = %s", (first,)
            )
    else:
        _close(store, first, drop)
    eligible, skipped = td.survey(store)
    assert eligible == []
    assert {"todo": second, "reason": "blocked"} in skipped
    with pytest.raises(BadInput, match="not done"):
        T.mint_todo_build(store, todo_id=second)
    assert _roots(store, second) == []
    _on(store)
    res = _run(store)
    assert res.extra["minted"] == [] and res.extra["split_closed"] == []
    assert _status(store, src) == "open"


def test_a_dropped_predecessor_holds_the_next_parts_build(store: Store) -> None:
    """The queue-time hold reads the same rule: minted while the
    predecessor was done, the next part's build is held again when the
    predecessor stops being done."""
    from precis.workers.executors._common import source_todo_holds

    _src, (first, second) = _split_source(store)
    _close(store, first, "done")
    build = T.mint_todo_build(store, todo_id=second).ids["build"]
    with store.pool.connection() as conn:
        assert source_todo_holds(conn, [build]) == {}
    _close(store, first, "won't-do")
    with store.pool.connection() as conn:
        held = source_todo_holds(conn, [build])
    assert f"td{second} is blocked-by td{first} (won't-do)" in held[build]


# -- failures -----------------------------------------------------------------


def test_mint_failure_does_not_stop_the_pass(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(store)
    first = _todo(store, "first", prio=1)
    second = _todo(store, "second", prio=2)
    real = td.mint_todo_build

    def flaky(st: Store, *a: Any, todo_id: int, **k: Any) -> Any:
        if todo_id == first:
            raise RuntimeError("the lane table is unreadable")
        return real(st, *a, todo_id=todo_id, **k)

    monkeypatch.setattr(td, "mint_todo_build", flaky)
    res = _run(store)
    assert (res.claimed, res.ok, res.failed) == (2, 1, 1)
    assert [m["todo"] for m in res.extra["minted"]] == [second]
    assert _roots(store, first) == [] and len(_roots(store, second)) == 1


def test_a_refused_mint_is_a_skip_not_a_failure(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.errors import BadInput

    _on(store)
    src = _todo(store)

    def refuse(*_a: Any, **_k: Any) -> Any:
        raise BadInput("todo-build: td1 already has a live chain (td2)")

    monkeypatch.setattr(td, "mint_todo_build", refuse)
    res = _run(store)
    assert (res.ok, res.failed) == (0, 0)
    assert res.extra["skipped"] == [
        {
            "todo": src,
            "reason": "mint-refused",
            "detail": "todo-build: td1 already has a live chain (td2)",
        }
    ]


# -- the pass report in worker_logs -------------------------------------------


def _payload_rows(
    caplog: pytest.LogCaptureFixture, handler: str
) -> list[dict[str, Any]]:
    """The ``extra={'payload': …}`` rows a pass logged under ``handler``."""
    return [
        p
        for r in caplog.records
        if (p := getattr(r, "payload", None)) is not None
        and p.get("handler") == handler
    ]


def test_the_pass_report_lands_as_a_worker_logs_payload_row(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    """gr751763: the per-todo result reads off the host — the pass's
    summary line carries the BatchResult as ``extra={'payload': …}``, so
    ``precis logs --pass todo_dispatch --payload`` and ``/logs`` show why
    a tagged todo was skipped."""
    _on(store)
    skipped = _todo(store, "not open", status="doing")
    minted = _todo(store)
    with caplog.at_level(logging.INFO, logger="precis.workers.todo_dispatch"):
        res = _run(store)
    assert (res.claimed, res.ok, res.failed) == (1, 1, 0)
    [payload] = _payload_rows(caplog, "todo_dispatch")
    assert payload["skipped"] == [{"todo": skipped, "reason": "not-open"}]
    assert payload["skipped_total"] == 1
    assert payload["minted"] == res.extra["minted"]
    assert payload["minted"][0]["todo"] == minted
    for key in (
        "base",
        "free",
        "open_todo",
        "budget",
        "refused",
        "refusals_applied",
        "released",
        "closed",
    ):
        assert key in payload
    # The enclosing cadence's own row attributes the wall time already.
    assert "duration_ms" not in payload


def test_a_no_op_outcome_lands_a_payload_row_too(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    """A pass that did nothing still says why in the same place."""
    _on(store)
    with caplog.at_level(logging.INFO, logger="precis.workers.todo_dispatch"):
        _run(store, lit=False)
    [payload] = _payload_rows(caplog, "todo_dispatch")
    assert payload["enabled"] is True and payload["gripe_pass"] == "not lit"
    assert (payload["claimed"], payload["ok"], payload["failed"]) == (0, 0, 0)


def test_readers_show_skip_beyond_first_fifty(
    store: Store,
    hub: Hub,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _on(store, base=0)
    for i in range(51):
        _todo(store, f"in progress {i}", status="doing")
    target = _todo(store)
    handler = BufferedDBLogHandler(
        _active_dsn(), host_name="dispatch-report-test", process_name="test-runner"
    )
    td.log.addHandler(handler)
    try:
        with caplog.at_level(logging.INFO, logger=td.log.name):
            _run(store, lit=False)
            _run(store)
    finally:
        td.log.removeHandler(handler)
        handler.close()

    jobs = JobHandler(hub=hub)
    view = jobs.get(id=f"/logs?todo=td{target}&host=dispatch-report-test").body
    expected = {"todo": target, "reason": "budget"}
    assert f"payload={json.dumps({'skipped': expected})}" in view
    assert "1 rows shown of 2 matching" in view
    absent = jobs.get(id="/logs?todo=td99999999&host=dispatch-report-test").body
    assert "no recorded decision in this report" in absent

    rows = cli_logs._query_logs(
        store,
        since_interval="1 hour",
        since_iso=None,
        host="dispatch-report-test",
        process=None,
        pass_name="todo_dispatch",
        level="INFO",
        limit=1,
    )
    cli_logs._render(rows, format="json", with_payload=True)
    [row] = json.loads(capsys.readouterr().out)
    assert row["payload"]["skipped_total"] == 52
    assert row["payload"]["skipped"][-1] == expected


# -- the scheduler call -------------------------------------------------------


def test_a_raising_todo_pass_leaves_the_gripe_pass_alone(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    gripe = _gripe_result(slots=2, picked=0, parked_full=False)
    seen: list[Any] = []

    def gripe_pass(st: Store, **_k: Any) -> BatchResult:
        seen.append("gripe")
        return gripe

    def todo_pass(st: Store, *, offer: td.GripeOffer, **_k: Any) -> BatchResult:
        seen.append(offer)
        raise RuntimeError("the todo pass blew up")

    monkeypatch.setattr(gd, "run_gripe_dispatch_pass", gripe_pass)
    monkeypatch.setattr(td, "run_todo_dispatch_pass", todo_pass)
    scheduler._run_gripe_dispatch(store, 1)  # returns quietly
    assert seen == ["gripe", td.GripeOffer(True, True, 2)]


def test_new_roots_reach_the_plan_tick(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As the gripe pass does after a mint, the new roots are queued for
    the plan tick at once."""
    from precis.workers.job_types import plan_dispatch

    seen: list[list[int]] = []
    monkeypatch.setattr(
        plan_dispatch, "enqueue_roots", lambda _store, roots: seen.append(list(roots))
    )
    _on(store)
    _todo(store, "a")
    _todo(store, "b")
    res = _run(store)
    assert seen == [[m["root"] for m in res.extra["minted"]]] and len(seen[0]) == 2
    # nothing minted, nothing queued
    _run(store)
    assert len(seen) == 1
