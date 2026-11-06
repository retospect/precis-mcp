"""gripe_dispatch: the dark factory's intake pass (td564019 cut 3)."""

from __future__ import annotations

from typing import Any

import pytest
from psycopg.types.json import Jsonb

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
    return int(ref.id)


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
    hot = _gripe(store, "hot", prio=2)
    old = _gripe(store, "old", age_minutes=600)
    newer = _gripe(store, "newer", age_minutes=61)
    _gripe(store, "human", meta=_HUMAN, prio=1)
    _gripe(store, "too young", age_minutes=5)
    _gripe(store, "worker-filed", meta=_WORKER, prio=1)
    _gripe(store, "no provenance", meta={}, prio=1)
    _gripe(store, "not code", tags=("fix:docs",))
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


def test_pass_mints_within_free_slots_and_is_idempotent(
    store: Store, two_lanes: Any
) -> None:
    _light(store)
    a = _gripe(store, "a", prio=1)
    b = _gripe(store, "b", prio=2)
    c = _gripe(store, "c", prio=3)
    assert gd.open_chain_count(store) == 0
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    # capacity 3, no open chains, but at most two per fire
    assert (res.claimed, res.ok, res.failed) == (2, 2, 0)
    assert res.extra["slots"] == 2 and res.extra["lane_capacity"] == 3
    assert len(_roots_for(store, a)) == 1 and len(_roots_for(store, b)) == 1
    assert _roots_for(store, c) == []
    [note] = _comments(store, a)
    assert note.startswith(
        f"dispatch: minted gripe-fix-direct td{_roots_for(store, a)[0]}"
    )
    assert gd.open_chain_count(store) == 2

    # Second fire: a and b are linked (skipped), one slot left → c.
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert (res.claimed, res.ok) == (1, 1)
    assert len(_roots_for(store, c)) == 1
    assert len(_roots_for(store, a)) == 1  # never doubled
    assert gd.open_chain_count(store) == 3

    # Third fire: lanes full, nothing minted.
    _gripe(store, "d", prio=1)
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert (res.claimed, res.ok) == (0, 0) and res.extra["slots"] == 0


def test_closed_chain_frees_its_slot_but_its_gripe_stays_out(
    store: Store, two_lanes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex, _sub = two_lanes
    use_lanes(monkeypatch, codex)  # capacity 1
    _light(store)
    a = _gripe(store, "a", prio=1)
    b = _gripe(store, "b", prio=2)
    assert gd.run_gripe_dispatch_pass(store, host="h1").ok == 1
    [root] = _roots_for(store, a)
    assert gd.run_gripe_dispatch_pass(store, host="h1").ok == 0  # full
    store.add_tag(
        root, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
    )
    assert gd.open_chain_count(store) == 0
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert res.ok == 1 and _roots_for(store, b)
    # a still has its (closed) chain linked: not re-minted
    assert _roots_for(store, a) == [root]


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


def test_cadence_and_service_spec() -> None:
    from precis.workers.registry import SERVICES_BY_NAME, ServiceKind
    from precis.workers.scheduler import CADENCES

    cad = next(c for c in CADENCES if c.name == "gripe_dispatch")
    assert cad.interval_s == 10 * 60
    # No service= gate: a dark pass must still win its lease, else the
    # cadence-staleness check reads it as "stopped firing" (al567129).
    assert cad.service is None and cad.spends is False
    spec = SERVICES_BY_NAME["gripe_dispatch"]
    assert spec.kind is ServiceKind.PASS
    assert not spec.default_profiles and spec.ref_pass is False
    assert spec.uses_model is False
